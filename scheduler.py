"""调度器：定时播报循环 + 警报轮询循环。

- 播报：按每个任务的发送时刻（``HH:MM``）计算「下一个未来时刻」，漏过不补发；
- 警报：每个任务按自身间隔独立计时，条件满足时投递；
- 单次执行异常只记一次日志，不中断循环；配置更新时立即重算。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncio
import contextlib
import logging

from .config import MealCardConfig, to_alert_tasks, to_broadcast_tasks
from .conditions import evaluate
from .delivery import broadcast_content
from .llm_rewrite import LlmCallError, generate_rewrite
from .platforms import MetricValue
from .query_engine import clear_query_cache, describe_platforms, run_all
from .renderer import build_image
from .text_report import render_text_report

logger = logging.getLogger(__name__)

_IDLE_WAIT_SECONDS = 60.0
"""没有任何任务时的空转检查间隔（秒）。"""


class ScheduleLoop:
    """插件后台调度循环。"""

    def __init__(self, plugin: Any) -> None:
        """初始化调度循环。

        Args:
            plugin: 所属插件实例（用于读取最新配置、上下文与缓存）。
        """

        self._plugin = plugin
        self._task: Optional[asyncio.Task] = None
        self._reload_event: Optional[asyncio.Event] = None
        self._alert_next_run: Dict[int, float] = {}
        self._broadcast_last_fired: Dict[str, datetime] = {}
        self._logged_invalid_conditions: set = set()

    @property
    def _ctx(self) -> Any:
        return self._plugin.ctx

    @property
    def _config(self) -> MealCardConfig:
        return self._plugin.config

    def start(self) -> None:
        """启动调度循环。"""

        if self._task is not None:
            return
        self._reload_event = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止调度循环。"""

        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._reload_event = None

    def notify_config_updated(self) -> None:
        """配置更新后唤醒循环立即重算。"""

        clear_query_cache()
        self._alert_next_run.clear()
        self._broadcast_last_fired.clear()
        if self._reload_event is not None:
            self._reload_event.set()

    async def _run(self) -> None:
        """主循环。"""

        while True:
            try:
                wait_seconds = self._seconds_until_next_event()
                await self._wait_or_reload(wait_seconds)
                if self._reload_event is not None and self._reload_event.is_set():
                    self._reload_event.clear()
                    continue
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[meal_card] 调度循环异常（本次跳过）：%s", exc, exc_info=True)
                await self._wait_or_reload(_IDLE_WAIT_SECONDS)

    async def _wait_or_reload(self, seconds: float) -> None:
        """等待指定秒数，或被配置更新事件提前唤醒。"""

        if self._reload_event is None:
            await asyncio.sleep(max(seconds, 0.0))
            return
        reload_task = asyncio.create_task(self._reload_event.wait())
        try:
            await asyncio.wait({reload_task}, timeout=max(seconds, 0.0))
        finally:
            reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reload_task

    def _seconds_until_next_event(self) -> float:
        """计算距离下一个事件（播报时刻或警报检查点）的秒数。"""

        now = datetime.now()
        candidates: List[float] = []

        for task in to_broadcast_tasks(self._config):
            if not task.enabled or not task.times:
                continue
            next_run = _next_broadcast_datetime(now, task.times)
            if next_run is not None:
                candidates.append(max((next_run - now).total_seconds(), 0.0))

        alert_tasks = [task for task in to_alert_tasks(self._config) if task.enabled]
        if alert_tasks:
            now_monotonic = _monotonic()
            for index, task in enumerate(alert_tasks):
                next_at = self._alert_next_run.get(index)
                if next_at is None:
                    next_at = now_monotonic
                    self._alert_next_run[index] = next_at
                candidates.append(max(next_at - now_monotonic, 0.0))

        if not candidates:
            return _IDLE_WAIT_SECONDS
        return max(min(candidates), 0.5)

    async def _tick(self) -> None:
        """执行到期的播报与警报任务。"""

        now = datetime.now()
        await self._run_due_broadcasts(now)
        await self._run_due_alerts()

    async def _run_due_broadcasts(self, now: datetime) -> None:
        """执行到期的定时播报任务。"""

        for task in to_broadcast_tasks(self._config):
            if not task.enabled or not task.times:
                continue
            due_slot = _due_slot(now, task.times)
            if due_slot is None:
                continue
            if self._broadcast_last_fired.get(task.name) == due_slot:
                continue
            if not task.targets:
                logger.warning("[meal_card] 播报任务 %s 未配置目标，已跳过", task.name)
                continue
            self._broadcast_last_fired[task.name] = due_slot
            await self._execute_task(
                name=task.name,
                targets=task.targets,
                keys=task.keys,
                condition=task.condition,
                send_mode=task.send_mode,
                content=task.content,
            )

    async def _run_due_alerts(self) -> None:
        """执行到期的警报任务。"""

        now_monotonic = _monotonic()
        alert_tasks = [task for task in to_alert_tasks(self._config) if task.enabled]
        for index, task in enumerate(alert_tasks):
            next_at = self._alert_next_run.get(index)
            if next_at is None:
                self._alert_next_run[index] = now_monotonic + task.interval_seconds
                continue
            if now_monotonic < next_at:
                continue
            self._alert_next_run[index] = now_monotonic + task.interval_seconds
            if not task.targets:
                logger.warning("[meal_card] 警报任务 %s 未配置目标，已跳过", task.name)
                continue
            await self._execute_task(
                name=task.name,
                targets=task.targets,
                keys=task.keys,
                condition=task.condition,
                send_mode=task.send_mode,
                content=task.content,
            )

    async def _execute_task(
        self,
        *,
        name: str,
        targets: Sequence[Any],
        keys: Sequence[str],
        condition: str,
        send_mode: str,
        content: str,
    ) -> None:
        """执行单个任务：查询 → 条件判断 → 生成内容 → 投递。"""

        try:
            results = await run_all(self._ctx, self._config, keys)
        except Exception as exc:
            logger.error("[meal_card] 任务 %s 查询失败：%s", name, exc, exc_info=True)
            self._record_result(name, f"查询失败：{exc}")
            return

        if not results:
            logger.warning("[meal_card] 任务 %s 没有可用的指标，已跳过", name)
            self._record_result(name, "没有可用的指标")
            return

        values: Dict[str, float] = {}
        for metric in results:
            if not (metric.ok and metric.value is not None):
                continue
            values[metric.qualified_id] = metric.value
            values.setdefault(metric.id, metric.value)
        matched, error = evaluate(condition, values)
        if error:
            if (name, error) not in self._logged_invalid_conditions:
                self._logged_invalid_conditions.add((name, error))
                logger.error("[meal_card] 任务 %s 的条件求值失败：%s", name, error)
            self._record_result(name, f"条件求值失败：{error}")
            return
        if not matched:
            logger.debug("[meal_card] 任务 %s 条件未满足，跳过本次发送", name)
            self._record_result(name, "条件未满足，未发送")
            return

        text, image_base64 = await self._build_content(results, send_mode, content)
        if not text and not image_base64:
            logger.error("[meal_card] 任务 %s 未能生成任何内容，已跳过", name)
            self._record_result(name, "生成内容失败")
            return

        success_count, failure_count = await broadcast_content(self._ctx, targets, text, image_base64)
        logger.info("[meal_card] 任务 %s 已执行：成功 %d 个目标，失败 %d 个目标", name, success_count, failure_count)
        self._record_result(name, f"已发送（成功 {success_count} / 失败 {failure_count}）")

    def _record_result(self, name: str, result: str) -> None:
        """把任务最近一次执行结果记录到插件内存（供日志与排查使用）。"""

        try:
            self._plugin._last_task_results[name] = result
        except Exception:  # pragma: no cover - 插件未初始化该字段时忽略
            pass

    async def _build_content(
        self,
        results: Sequence[MetricValue],
        send_mode: str,
        content: str,
    ) -> Tuple[str, Optional[str]]:
        """按发送方式生成内容，返回 ``(文本, 图片 base64)``。"""

        mode = str(send_mode or "template").strip().lower()
        template_override = content.strip() or None
        platform_name = self._platform_name()

        if mode == "image":
            image_base64, render_error = await build_image(self._ctx, results, self._config, platform_name)
            if image_base64 is not None:
                return "", image_base64
            logger.error("[meal_card] 图片渲染失败，已回退为文本：%s", render_error)
            return render_text_report(results, self._config, platform_name, template_override), None

        if mode == "llm":
            try:
                return await generate_rewrite(self._ctx, self._config, results, platform_name), None
            except LlmCallError as exc:
                logger.error("[meal_card] LLM 转述失败，已回退为文本：%s", exc)
                return render_text_report(results, self._config, platform_name, template_override), None

        return render_text_report(results, self._config, platform_name, template_override), None

    def _platform_name(self) -> str:
        """返回当前启用平台的中文描述（用于报告与模板占位符）。"""

        return describe_platforms(self._config)


def _monotonic() -> float:
    """返回单调时钟秒数。"""

    import time

    return time.monotonic()


def _next_broadcast_datetime(now: datetime, times: Sequence[Any]) -> Optional[datetime]:
    """计算下一个未来播报时刻。"""

    today_candidates = [
        now.replace(hour=item.hour, minute=item.minute, second=0, microsecond=0)
        for item in times
        if now.replace(hour=item.hour, minute=item.minute, second=0, microsecond=0) > now
    ]
    if today_candidates:
        return min(today_candidates)
    if not times:
        return None
    return datetime.combine(now.date() + timedelta(days=1), min(times))


def _due_slot(now: datetime, times: Sequence[Any]) -> Optional[datetime]:
    """返回当前时刻命中的播报槽位（精确到分钟）；未命中时返回 None。

    用于防止同一个播报时刻被重复触发（同一分钟内只触发一次）。
    """

    for item in times:
        scheduled = now.replace(hour=item.hour, minute=item.minute, second=0, microsecond=0)
        delta = (now - scheduled).total_seconds()
        if 0 <= delta < _IDLE_WAIT_SECONDS:
            return scheduled
    return None
