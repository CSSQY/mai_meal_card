"""历史采样存储与曲线数据生成。

本模块为 WebUI「麦麦饭卡」页面提供数据支撑：

- :class:`HistoryStore`：把每次检查（采样）得到的指标数值持久化到插件数据目录
  （``data/plugins/<插件ID>/history.json``），支持按「检查次数 / 天数 / 文件大小」三种方式
  裁剪历史；写入采用「临时文件 + 原子替换」，避免中途异常损坏文件。
- :func:`build_curve`：把历史序列重采样为平滑曲线（Catmull-Rom 转三次贝塞尔等价的
  样条插值），供宿主前端的折线图使用（宿主折线是直线段，靠加密采样点呈现平滑效果）。

存储结构（JSON）::

    {
      "version": 1,
      "metric_meta": {"minimax.interval_remaining": {"name": "5h窗口剩余", "platform": "…", "kind": "percent", "unit": ""}},
      "samples": [{"ts": 1728662400, "time": "2026-10-11 23:00:00", "values": {"minimax.interval_remaining": 85.0}}]
    }
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import logging
import os

logger = logging.getLogger(__name__)

STORAGE_VERSION = 1
"""存储结构版本号。"""

DEFAULT_CHART_POINTS = 120
"""曲线最多输出的点数（宿主前端单图上限 2000，这里取更易读的密度）。"""

_MAX_SAMPLES = 200000
"""内存中样本数的硬上限，避免异常配置导致内存膨胀。"""


@dataclass(frozen=True)
class RetentionPolicy:
    """历史数据裁剪策略。

    ``mode`` 决定哪一项生效：``count`` 按检查次数、``days`` 按天数、``size`` 按文件大小。
    """

    mode: str = "count"
    count: int = 2880
    days: int = 30
    size_mb: float = 5.0


def format_size(num_bytes: int) -> str:
    """把字节数格式化为可读文本（如 ``12.3 KB``）。"""

    size = max(int(num_bytes), 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def format_span(start: Optional[int], end: Optional[int]) -> str:
    """把起止时间戳格式化为「起 ~ 止」文本；无数据时返回空串。"""

    if not start or not end:
        return ""
    return (
        f"{datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M')}"
        f" ~ {datetime.fromtimestamp(end).strftime('%m-%d %H:%M')}"
    )


class HistoryStore:
    """插件历史采样存储（单文件 JSON）。"""

    def __init__(self, path: Path) -> None:
        """初始化存储并立即读取磁盘内容。

        Args:
            path: 存储文件路径（位于插件持久化数据目录内）。
        """

        self._path = Path(path)
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._samples: List[Dict[str, Any]] = []
        self.load()

    # ──── 读 ────

    def load(self) -> None:
        """从磁盘读取历史；文件缺失或损坏时回退为空历史（不影响插件运行）。"""

        self._meta = {}
        self._samples = []
        try:
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - 坏文件不能拖垮插件
            logger.warning("[meal_card] 历史记录文件读取失败，已按空历史处理：%s", exc)
            return
        if not isinstance(payload, dict):
            logger.warning("[meal_card] 历史记录文件结构异常，已按空历史处理")
            return

        raw_meta = payload.get("metric_meta")
        if isinstance(raw_meta, dict):
            self._meta = {
                str(key): dict(value) for key, value in raw_meta.items() if isinstance(value, dict)
            }
        raw_samples = payload.get("samples")
        if isinstance(raw_samples, list):
            self._samples = [
                item
                for item in raw_samples
                if isinstance(item, dict) and isinstance(item.get("values"), dict)
            ]

    # ──── 写 ────

    def append(
        self,
        values: Dict[str, float],
        meta: Dict[str, Dict[str, Any]],
        *,
        at: Optional[datetime] = None,
        policy: Optional[RetentionPolicy] = None,
    ) -> bool:
        """追加一条采样并按策略裁剪、落盘。

        Args:
            values: ``{指标 id: 数值}``（只含本次取值成功的数值指标）。
            meta: ``{指标 id: {name, platform, kind, unit}}``，用于页面展示指标名与单位。
            at: 采样时刻；留空取当前时间。
            policy: 裁剪策略；留空按默认策略。

        Returns:
            bool: 是否真正写入了样本（无有效数值时返回 ``False``）。
        """

        if not values:
            return False

        moment = at or datetime.now()
        for metric_id, info in meta.items():
            if isinstance(info, dict):
                self._meta[str(metric_id)] = dict(info)
        self._samples.append(
            {
                "ts": int(moment.timestamp()),
                "time": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "values": {str(key): float(value) for key, value in values.items()},
            }
        )
        self._prune(policy or RetentionPolicy())
        try:
            self.save()
        except Exception as exc:  # noqa: BLE001 - 写盘失败只记日志
            logger.error("[meal_card] 历史记录写入失败：%s", exc)
            return False
        return True

    def save(self) -> None:
        """原子写入磁盘（先写临时文件再替换，避免半截文件）。"""

        payload = {
            "version": STORAGE_VERSION,
            "metric_meta": self._meta,
            "samples": self._samples,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.parent / f"{self._path.name}.tmp"
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, self._path)

    def clear(self) -> None:
        """清空历史样本（保留指标元信息）并落盘。"""

        self._samples = []
        try:
            self.save()
        except Exception as exc:  # noqa: BLE001
            logger.error("[meal_card] 历史记录清空后写入失败：%s", exc)

    # ──── 裁剪 ────

    def _prune(self, policy: RetentionPolicy) -> None:
        """按策略裁剪样本（至少保留 1 条）。"""

        if len(self._samples) > _MAX_SAMPLES:
            self._samples = self._samples[-_MAX_SAMPLES:]

        mode = str(policy.mode or "count").strip().lower()
        if mode == "days":
            self._prune_by_days(max(int(policy.days), 1))
        elif mode == "size":
            self._prune_by_size(max(float(policy.size_mb), 0.1))
        else:
            self._samples = self._samples[-max(int(policy.count), 1) :]

    def _prune_by_days(self, days: int) -> None:
        """丢弃早于 ``days`` 天的样本。"""

        cutoff = int(datetime.now().timestamp()) - days * 86400
        kept = [item for item in self._samples if int(item.get("ts", 0)) >= cutoff]
        self._samples = kept or self._samples[-1:]

    def _prune_by_size(self, size_mb: float) -> None:
        """从最旧开始丢弃样本，直到序列化体积不超过 ``size_mb``。"""

        limit = int(size_mb * 1024 * 1024)
        while len(self._samples) > 1 and self._estimate_bytes() > limit:
            drop = max(1, len(self._samples) // 20)
            self._samples = self._samples[drop:]

    def _estimate_bytes(self) -> int:
        """估算当前数据序列化后的字节数（中文按 UTF-8 多字节计算）。"""

        return len(self._serialize().encode("utf-8"))

    def _serialize(self) -> str:
        """返回当前数据的 JSON 文本（用于估算文件大小）。"""

        return json.dumps(
            {"version": STORAGE_VERSION, "metric_meta": self._meta, "samples": self._samples},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # ──── 查询 ────

    @property
    def meta(self) -> Dict[str, Dict[str, Any]]:
        """指标元信息（``{指标 id: 名称/平台/类型/单位}``）。"""

        return self._meta

    @property
    def samples(self) -> List[Dict[str, Any]]:
        """全部采样样本（按时间升序）。"""

        return self._samples

    def latest(self) -> Optional[Dict[str, Any]]:
        """最近一条采样；无数据时返回 ``None``。"""

        return self._samples[-1] if self._samples else None

    def stats(self) -> Dict[str, Any]:
        """返回概览统计：样本数、起止时间、文件体积。"""

        try:
            file_size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            file_size = 0
        return {
            "count": len(self._samples),
            "first_ts": int(self._samples[0].get("ts", 0)) if self._samples else 0,
            "last_ts": int(self._samples[-1].get("ts", 0)) if self._samples else 0,
            "file_size": file_size,
            "metric_count": len(self._meta),
        }

    def series(self, metric_id: str) -> List[Tuple[int, float]]:
        """取出某个指标的 ``(时间戳, 数值)`` 序列。"""

        points: List[Tuple[int, float]] = []
        for item in self._samples:
            raw = item.get("values", {}).get(metric_id)
            if isinstance(raw, (int, float)):
                points.append((int(item.get("ts", 0)), float(raw)))
        return points


# ────────────────────────── 指标选择 ──────────────────────────


def resolve_metric_id(meta: Dict[str, Dict[str, Any]], requested: str = "") -> str:
    """解析要绘制的指标 id。

    匹配顺序：完整 id → 后缀匹配（``interval_remaining`` 命中 ``minimax.interval_remaining``）
    → 包含匹配；``requested`` 留空时自动挑选「第一个百分比指标」，没有则取第一个指标。
    """

    keys = [key for key in meta.keys()]
    wanted = str(requested or "").strip()
    if wanted:
        if wanted in meta:
            return wanted
        suffix = f".{wanted}"
        for key in keys:
            if key.endswith(suffix):
                return key
        for key in keys:
            if wanted in key:
                return key
        return ""

    for key, info in meta.items():
        if str((info or {}).get("kind", "")).strip().lower() == "percent":
            return key
    return keys[0] if keys else ""


# ────────────────────────── 平滑曲线 ──────────────────────────


def _bucket_average(points: Sequence[Tuple[int, float]], target: int) -> List[Tuple[int, float]]:
    """按序号分桶取平均，把序列压缩到约 ``target`` 个控制点。"""

    if target <= 0 or len(points) <= target:
        return list(points)
    result: List[Tuple[int, float]] = []
    size = len(points) / target
    for index in range(target):
        start = int(index * size)
        end = max(start + 1, int((index + 1) * size))
        chunk = points[start:end]
        if not chunk:
            continue
        average_ts = int(sum(item[0] for item in chunk) / len(chunk))
        average_value = sum(item[1] for item in chunk) / len(chunk)
        result.append((average_ts, average_value))
    return result


def _catmull_rom(points: Sequence[Tuple[int, float]], samples_per_segment: int) -> List[Tuple[int, float]]:
    """用 Catmull-Rom 样条在控制点之间插值，生成平滑的加密序列。"""

    count = len(points)
    if count < 3 or samples_per_segment < 1:
        return list(points)

    padded = [points[0]] + list(points) + [points[-1]]
    output: List[Tuple[int, float]] = []
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1], padded[index], padded[index + 1], padded[index + 2]
        for step in range(samples_per_segment):
            t = step / samples_per_segment
            t2 = t * t
            t3 = t2 * t
            value = 0.5 * (
                2 * p1[1]
                + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            timestamp = p1[0] + (p2[0] - p1[0]) * t
            output.append((int(timestamp), value))
    output.append((points[-1][0], points[-1][1]))
    return output


def build_curve(
    points: Sequence[Tuple[int, float]],
    *,
    max_points: int = DEFAULT_CHART_POINTS,
    label_format: str = "%m-%d %H:%M",
) -> List[Dict[str, Any]]:
    """把 ``(时间戳, 数值)`` 序列转换为宿主折线图可直接使用的最平滑数据。

    宿主前端的折线是直线段，因此这里先压缩控制点、再用样条插值加密，使折线呈现平滑曲线。

    Args:
        points: 时间升序的序列；点数不足时原样输出。
        max_points: 输出点数上限（宿主前端单图上限为 2000）。
        label_format: 横轴标签的时间格式。

    Returns:
        List[Dict[str, Any]]: ``[{"label": "10-11 23:00", "value": 85.0}, ...]``。
    """

    if not points:
        return []

    limit = max(int(max_points), 2)
    ordered = sorted(points, key=lambda item: item[0])
    if len(ordered) > limit:
        controls = _bucket_average(ordered, max(limit // 2, 3))
    else:
        controls = list(ordered)

    if len(controls) >= 3:
        # 每个控制点区间最多扩成 4 段（够顺滑又不超上限）
        room = max(1, limit // max(len(controls) - 1, 1))
        smoothed = _catmull_rom(controls, min(4, room))
    else:
        smoothed = controls

    if len(smoothed) > limit:
        step = len(smoothed) / limit
        smoothed = [smoothed[int(index * step)] for index in range(limit)]

    return [
        {"label": datetime.fromtimestamp(ts).strftime(label_format), "value": round(value, 4)}
        for ts, value in smoothed
    ]


def summarize_sample(
    sample: Dict[str, Any],
    meta: Dict[str, Dict[str, Any]],
    *,
    max_items: int = 3,
) -> str:
    """把一条采样压缩成一行摘要（用于历史表格）。"""

    values = sample.get("values", {})
    parts: List[str] = []
    for index, (metric_id, value) in enumerate(values.items()):
        if index >= max_items:
            remaining = len(values) - max_items
            if remaining > 0:
                parts.append(f"等 {remaining} 项")
            break
        name = str((meta.get(metric_id) or {}).get("name") or metric_id)
        parts.append(f"{name} {value:g}")
    return " / ".join(parts)


def iter_recent_samples(samples: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """取最近 ``limit`` 条采样（倒序，最新在前）。"""

    items = list(samples)
    return list(reversed(items[-max(int(limit), 1) :]))
