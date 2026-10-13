# 麦麦饭卡（MaiBot 插件）
>本插件由AI主导开发

> 额度 / 用量查询与播报：LLM 工具 + `/饭卡` 指令 + 定时播报 + 条件警报。

- 插件 ID：`cssqy.mai-meal-card`
- 目录：`plugins/mai_meal_card/`
- 宿主版本要求：`1.3.0 ~ 1.3.99`；SDK 要求：`2.10.0 ~ 2.99.99`
- 内置平台：**MiniMax TokenPlan**、**DeepSeek 开放平台**；另支持**自定义平台**

## 1. 功能特性

- **槽位式配置**：平台 / 播报 / 警报各自用「数量控制」决定显示几个槽位，每个槽位都是独立表单，
  不需要手写「分号分隔字符串」。
- **支持多个平台**：可同时配置多个平台，查询时逐个取值；指标 id 带平台前缀
  （如 `minimax.interval_remaining`），条件与权限都按这个 id 书写。
- **支持自定义平台**：平台类型选 `custom` 后自行填写接口地址、鉴权、取值表达式等，无需等待内置适配。
- **支持 DeepSeek 开放平台**：填 API Key 即可查账户总余额 / 赠金余额 / 充值余额（金额带币种）。
- **多平台分组展示**：同时查询多个平台时，图片、文本与 LLM 转述都会按平台分组并标注平台名，一眼分清数据来源。
- **平台适配器架构**：平台差异全部收敛在 `platforms/` 目录，新增内置平台只需实现一个适配器并注册。
- **LLM 工具 `query_meal_card`**：让智能体查询额度 / 重置倒计时，可按指标与类型过滤。
- **`/饭卡` 指令**：可选择模板文本 / LLM 风格化转述 / 图片渲染，支持指定指标。
- **定时播报**：按 `HH:MM` 时刻向多个 QQ 群 / QQ 用户推送，支持可选 `and/or/not` 触发条件。
- **条件警报**：按查询间隔轮询，条件满足时推送告警。
- **两组独立权限**：LLM 工具查询权限 与 `/饭卡` 指令查询权限，各含黑白名单 + 放行指标 + 每对话覆盖。
- **三种呈现**：文本模板（占位符）、LLM 风格化转述（宿主模型组或自定义 [OI] 兼容服务）、
  图片渲染（HTML 模板 + 占位符，可添加自定义模板文件）。
- **图片页脚可配置**：可关闭图片底部页脚，或自定义页脚文案（默认「由麦麦饭卡插件渲染」）。
- **WebUI 页面「麦麦饭卡」**：侧边栏插件页面展示各平台当前额度与用量、历史趋势**平滑曲线**
  与最近采样记录；**可同时追踪多个指标**（`[history].chart_metrics` 里增删，最多 6 条），
  曲线颗粒度由「检查周期」控制，历史数据可按次数 / 天数 / 文件大小保留。

## 2. 安装与启用

1. 把整个 `mai_meal_card/` 目录放到 MaiBot 的 `plugins/` 下（保持目录名不变）。
2. 启动 / 重启 MaiBot；插件会在插件运行时（Runner）进程里被自动发现并加载。
3. 首次加载时，Runner 会按插件配置模型自动生成 `plugins/mai_meal_card/config.toml`，
   之后修改该文件即可热更新配置（无需重启）。
4. 在 WebUI 的插件配置页按标签页填写（平台 / 播报 / 警报 / 展示与模板 / 记录与统计 / 指令与权限 / 渲染与上限）。

依赖说明：插件只使用宿主提供的 `maibot-plugin-sdk`、宿主自带的 `httpx` 与 `pillow`。

## 3. 槽位机制（重要）

平台、播报、警报都采用「**数量控制 + 固定槽位**」：

| 数量控制字段 | 生成的槽位分节 |
|---|---|
| `[plugin].platform_count` | `[platform1]` … `[platformN]` |
| `[plugin].broadcast_count` | `[broadcast1]` … `[broadcastN]` |
| `[plugin].alert_count` | `[alert1]` … `[alertN]` |

- 槽位在**插件加载时**按数量生成，因此：
  - **改大**数量后，需要保存并到插件管理处「**禁用再启用**」插件（或重启麦麦），才会出现新槽位；
  - **改小**数量后保存并重载，超出部分的槽位配置会被**清空**（临时停用某个槽位请用它自己的「启用」开关）。
- 每个槽位都是独立的表单分节，字段互相独立，不再需要拼接字符串。

## 4. 快速开始

### 4.1 配置一个 MiniMax 平台

在「平台」标签页的 `[platform1]` 里：

| 字段 | 填什么 |
|---|---|
| 平台类型 | `minimax` |
| 平台标识 | 建议填 `minimax`（会作为指标 id 前缀，如 `minimax.interval_remaining`） |
| 区域 | `cn`=国内版（platform.minimaxi.com）；`global`=国际版（platform.minimax.io） |
| 订阅密钥 | 你的 TokenPlan 订阅密钥（`sk-cp-…`） |

> 订阅密钥在「订阅管理 / Token Plan」页面获取；它与「按量付费」的普通 API Key **不通用**，填错会返回 401。

### 4.2 使用

- 群里发 `/饭卡`：按配置的发送方式（默认图片）回复全部指标。
- `/饭卡 minimax.interval_remaining,minimax.interval_reset`：只查指定的指标。
- `/饭卡 全部 文字`：查全部并用模板文本回复。
- 让 LLM 调用工具 `query_meal_card`（`query_key` / `metric`）。

### 4.3 再添加一个平台

把 `[plugin].platform_count` 改为 `2` → 保存 → 「禁用再启用」插件 → 出现 `[platform2]`，按需填写。

- 若是另一个 MiniMax 账号，把「平台标识」改成 `mm-global` 之类以免指标 id 冲突；
- 若是别的平台，可先选 `custom` 自行填写接口（见 4.4），等内置适配上线后再切换。

### 4.4 配置一个自定义平台

平台类型选 `custom` 后填写：

| 字段 | 说明 |
|---|---|
| 接口基地址 | 如 `https://api.example.com` |
| 接口路径 | 如 `/api/user/self`（可填完整 URL） |
| 请求方法 | `GET` / `POST` |
| 鉴权方式 | `none` / `bearer` / `header` / `basic` |
| 鉴权值 | `bearer` 填密钥；`header` 填「名=值」；`basic` 填「用户名:密码」 |
| 附加请求头 | 每条「名=值」，可加多条 |
| 取值表达式 | 返回 JSON 时填点号路径（如 `data.quota`）；纯文本数字留空 |
| 取值含义 | `quota` 额度 / `usage` 用量 / `other` 其他（决定展示方式） |
| 单位 | 如 `美元` / `积分` |
| 换算比例 | `a:b`（`a` 原始单位 = `b` 显示单位），留空按 1:1 |
| 上限表达式 | 可选，用于用量进度条 |

自定义平台固定产出 `value` 指标（指标 id 为 `<平台标识>.value`）与 `raw`（原始响应）。

### 4.5 配置一个 DeepSeek 平台

在「平台」标签页的 `[platformN]` 里：

| 字段 | 填什么 |
|---|---|
| 平台类型 | `deepseek` |
| 平台标识 | 建议填 `deepseek`（会作为指标 id 前缀，如 `deepseek.balance_total`） |
| API 密钥 | DeepSeek 开放平台 API Key（`sk-` 开头，在开放平台控制台创建） |

> 查询的是账户余额（总余额 / 赠金 / 充值余额），接口只读、不产生任何费用；
> 基地址默认 `https://api.deepseek.com`，一般无需填写。

## 5. 使用示例

### 5.1 `/饭卡` 指令

```
/饭卡 [指标] [全部|文字|图片|转述]
```

| 参数 | 说明 |
|---|---|
| 指标（可省略） | 逗号分隔的指标 id，如 `minimax.interval_remaining`；省略时用 `[command].default_query_keys`，若也为空则查全部 |
| `全部` | 查询全部指标 |
| `文字` / `图片` / `转述` | 覆盖本次发送方式（模板文本 / 图片 / LLM 转述） |

引用他人消息后发送 `/饭卡` 同样可用：宿主会把自己拼进消息文本的「引用原文」
（形如 `[回复了某某的消息: 原消息内容]`）一并传给插件，插件会在解析参数前**自动剥离引用内容**，
因此引用块不会被误当成指标 id。若引用的消息本身含有 `/饭卡`（例：别人引用你的旧消息再发言），
不会触发本指令。

### 5.2 LLM 工具 `query_meal_card`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query_key` | string | 否 | 指标 id，多个用逗号分隔；留空=全部 |
| `metric` | string | 否 | 指标类型过滤：`percent` / `duration` / `count` / `text` / `all` |

工具侧安全收口（独立于 `/饭卡` 指令的一组权限）：对话流名单 + 放行指标；
会话上下文（群号 / QQ 号）由宿主注入，**不由模型提供**。

返回示例（节选）：

```json
{
  "success": true,
  "content": "🍚 饭卡状态（2026-10-09 20:00:00）\n5h窗口剩余：[██████████░░░░░░░░░░] 52.5%\n5h窗口重置倒计时：2 小时 30 分钟",
  "items": [
    {"id": "minimax.interval_remaining", "name": "5h窗口剩余", "platform": "MiniMax TokenPlan（国内版）",
     "kind": "percent", "value": 52.5, "display": "[██████████░░░░░░░░░░] 52.5%", "ok": true}
  ]
}
```

### 5.3 WebUI 页面「麦麦饭卡」

在麦麦 WebUI 的侧边栏「插件扩展」中打开 **麦麦饭卡** 页面，可以看到：

| 区块 | 内容 |
|---|---|
| 记录状态 / 采样点数 / 检查周期（颗粒度）/ 记录文件 | 概览指标卡 |
| 记录概况 | 记录区间、保留策略、默认曲线指标、记录存储状态 |
| 当前额度与用量 | 各平台指标表：平台 / 指标 / 指标 id / 当前值（取最近一次采样） |
| 趋势曲线（平滑） | 「临时查看」输入指标 id → 点「应用指标」切换；下方按 `[history].chart_metrics` 逐条渲染**多条曲线**（每条一张图，带指标名标题） |
| 最近采样记录 | 最近 50 条采样：时间 / 指标数 / 摘要 |
| 操作 | 「立即采样」立刻补一条记录；「清空历史记录」删除全部历史（需确认） |

- 曲线**颗粒度 = 检查周期**（`[history].interval_seconds`）：周期越小曲线越细；
- **同时追踪多个指标**：在 `[history].chart_metrics` 里增删指标 id（最多 6 条），页面立即按新列表出图；
  未配置的曲线槽位在页面上不占位（由声明式 `when` 条件隐藏）；
- 页面所有数据来自**已记录的历史采样**，「当前值」即最近一次采样，点「立即采样」可立刻刷新；
- 指标 id 支持简写（如 `interval_remaining` 会匹配 `minimax.interval_remaining`）；
- 页面由宿主声明式组件渲染（`webui.json`），不注入自定义 JS/HTML/CSS。

## 6. 指标清单

### 6.1 MiniMax TokenPlan（平台类型 `minimax`）

指标 id 为 `<平台标识>.<指标>`，例如平台标识填 `minimax` 时：

| 指标 id | 含义 | 类型 |
|---|---|---|
| `minimax.interval_remaining` | 5h窗口剩余（5 小时滚动窗口） | 百分比 |
| `minimax.interval_reset` | 5h窗口重置倒计时 | 时长 |
| `minimax.weekly_remaining` | 本周窗口剩余 | 百分比 |
| `minimax.weekly_reset` | 周窗口重置倒计时 | 时长 |
| `minimax.interval_usage` | 5h窗口已用次数（计数制套餐才有） | 计数 |
| `minimax.model_<模型名>_remaining` | 各模型剩余（如 `minimax.model_video_remaining`） | 百分比 |
| `minimax.raw` | 原始响应摘要（排查用） | 文本 |

> 百分比字段的官方语义是「**剩余**」（不是已用）。
> 未填「平台标识」时会自动用 `platform1`、`platform2` 这类编号作为前缀。
> `general` 条目代表「通用/总体额度池」，已由「5h窗口剩余 / 本周窗口剩余」表达，
> 因此**不再单独产出** `model_general_remaining`（避免与总体指标重复）。

### 6.2 DeepSeek 开放平台（平台类型 `deepseek`）

接口：`GET https://api.deepseek.com/user/balance`（官方文档「查询余额」）。指标 id 为 `<平台标识>.<指标>`：

| 指标 id | 含义 | 类型 |
|---|---|---|
| `deepseek.balance_total` | 账户总余额（含赠金与充值余额） | 计数（单位=币种） |
| `deepseek.balance_granted` | 未过期的赠金余额 | 计数 |
| `deepseek.balance_topped_up` | 充值余额 | 计数 |
| `deepseek.available` | 账户可用状态（可用 / 不可用） | 文本 |
| `deepseek.raw` | 原始响应摘要（排查用） | 文本 |

> 金额单位取响应里的币种（`CNY` / `USD`）。响应含多个币种时，第一条按上表产出，
> 其余条目追加币种后缀（如 `deepseek.balance_total_usd`）。
> `available` 是文本指标、不参与条件表达式；做余额告警请用 `deepseek.balance_total < 10` 这类数值条件。

### 6.3 自定义平台（平台类型 `custom`）

| 指标 id | 含义 |
|---|---|
| `<平台标识>.value` | 按「取值含义」展示的额度 / 用量 |
| `<平台标识>.raw` | 原始响应文本 |

> 指标 id 也支持**不带前缀**的简写（如 `interval_remaining`），此时匹配所有平台下的该指标；
> 查询结果里只有一个平台时，模板也同时提供不带前缀的占位符。

## 7. 配置说明

配置文件为插件目录下的 `config.toml`（由 Runner 依据配置模型自动生成与补齐）。

### 7.1 `[plugin]` 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关 |
| `platform_count` | int | `1` | 平台槽位数量（改大需重载一次插件） |
| `broadcast_count` | int | `1` | 播报槽位数量（改大需重载一次插件） |
| `alert_count` | int | `1` | 警报槽位数量（改大需重载一次插件） |
| `config_version` | str | `"1.4.0"` | 配置版本（请勿手改） |

### 7.2 平台槽位 `[platformN]`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用此平台 |
| `adapter` | str | `"minimax"` | 平台类型：`minimax` / `deepseek` / `custom` |
| `key` | str | `""` | 平台标识（指标 id 前缀）；留空用 `platformN` |
| `name` | str | `""` | 显示名称；留空用平台类型名称 |

**平台类型 = `minimax` 时：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `region` | str | `"cn"` | `cn`=国内版；`global`=国际版（选错会 401） |
| `api_key` | str | `""` | TokenPlan 订阅密钥（`sk-cp` 开头） |
| `base_url` | str | `""` | 高级：自定义接口基地址，留空按区域自动选择 |

**平台类型 = `deepseek` 时：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `api_key` | str | `""` | DeepSeek 开放平台 API Key（`sk-` 开头） |
| `base_url` | str | `""` | 高级：留空用 `https://api.deepseek.com` |

> `region` 仅对 `minimax` 生效；`api_key` 与 `base_url` 为两种内置平台共用的字段。

**平台类型 = `custom` 时：**

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `custom_base_url` | str | `""` | 接口基地址（必填） |
| `custom_path` | str | `""` | 接口路径，可填完整 URL |
| `custom_method` | str | `"GET"` | `GET` / `POST` |
| `custom_auth_mode` | str | `"bearer"` | `none` / `bearer` / `header` / `basic` |
| `custom_auth_value` | str | `""` | 鉴权值 |
| `custom_headers` | list[str] | `[]` | 附加请求头，每条「名=值」 |
| `custom_value_expr` | str | `""` | 取值表达式（点号路径；纯文本留空） |
| `custom_value_type` | str | `"quota"` | `quota` / `usage` / `other` |
| `custom_unit` | str | `""` | 展示单位 |
| `custom_ratio` | str | `""` | 换算比例 `a:b` |
| `custom_limit_expr` | str | `""` | 上限表达式（用量进度条） |

### 7.3 播报槽位 `[broadcastN]`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用此播报 |
| `name` | str | `""` | 任务名（留空用「播报N」） |
| `times` | str | `"08:00"` | 每日发送时刻，`HH:MM`，多个用逗号（如 `08:00,20:00`）；漏过不补发 |
| `target_groups` | list[str] | `[]` | 接收播报的 QQ 群号 |
| `target_users` | list[str] | `[]` | 接收播报的 QQ 号（私聊） |
| `keys` | str | `""` | 指标 id，逗号分隔；留空或 `*` = 全部 |
| `mode` | str | `"image"` | `image` / `template` / `llm` |
| `condition` | str | `""` | 可选触发条件（留空=每次都发） |
| `content` | str | `""` | 可选自定义文本模板（留空用全局模板） |

### 7.4 警报槽位 `[alertN]`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用此警报 |
| `name` | str | `""` | 任务名（留空用「警报N」） |
| `interval_seconds` | int | `3600` | 查询间隔（秒），最小 5 |
| `target_groups` / `target_users` | list[str] | `[]` | 目标群号 / 目标 QQ 号 |
| `keys` | str | `""` | 参与判断与播报的指标 id；留空=全部 |
| `condition` | str | `""` | 触发条件（**必填**，留空则永不触发） |
| `mode` | str | `"llm"` | `llm` / `template` / `image` |
| `content` | str | `""` | 可选自定义文本模板 |

**条件表达式**：支持 `> < == >= <= !=` 与 `and/or/not` 及括号，例如
`minimax.interval_remaining < 20 and minimax.weekly_remaining < 50`。

### 7.5 `[display]` 数值展示

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `quota_decimals` | int | `2` | 计数类数值小数位；百分比固定 1 位 |
| `show_progress` | bool | `true` | 百分比指标是否显示进度条 |
| `show_percentage` | bool | `true` | 百分比指标是否显示数字；关闭后只显示进度条 |
| `progress_bar_width` | int | `20` | 进度条字符宽度（5~50） |
| `show_raw` | bool | `false` | 是否在文本模板、LLM 转述与 LLM 工具结果里展示「原始响应」（排查用） |

> **原始响应（`raw`）默认不展示**：它是平台返回的响应体摘要，
> 默认不出现在文本模板、LLM 转述与 LLM 工具结果里，**图片渲染始终不展示**。
> 需要排查接口异常时把 `show_raw` 打开即可；**失败状态的原始响应始终保留**（否则看不到失败原因）。

### 7.6 `[text_template]` 文本模板

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `template` | str | `🍚 饭卡状态（{date}）\n{items}\n{unavailable}` | 文本模板，占位符见 7.12 |

### 7.7 `[llm_rewrite]` LLM 风格化转述

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `provider` | str | `"host"` | `host`=复用宿主模型组；`custom`=调用自定义兼容服务 |
| `llm_task_name` | str | `"utils"` | `provider="host"` 时复用的宿主模型组 |
| `api_base_url` | str | `""` | `provider="custom"` 时的接口基地址（到 `/v1` 为止） |
| `api_key` | str | `""` | 自定义服务的 API Key；留空则不发送 `Authorization` 头 |
| `model` | str | `""` | 自定义服务的模型名 |
| `temperature` | float | `0.7` | 采样温度（0~2），仅自定义服务生效 |
| `extra_body` | str | `""` | 额外请求体参数（JSON 对象文本） |
| `max_retries` | int | `2` | 调用失败后的重试次数（0~10） |
| `timeout_seconds` | float | `60.0` | 单次请求超时（秒） |
| `retry_interval_seconds` | float | `1.0` | 两次重试之间的等待（秒） |
| `use_persona` | bool | `true` | 是否注入宿主人格 |
| `persona_template` | str | `""` | 人格提示词模板，占位符 `{bot_name}`、`{personality}`、`{reply_style}` |
| `persona_extra` | str | `""` | 额外提示词，追加在人格之后 |
| `llm_prompt` | str | 内置 | 转述任务说明 |

- `provider="custom"`：向 `<api_base_url>/chat/completions` 发 `POST`；网络异常与 5xx / 429 / 408 / 409 / 425 会重试，其余 4xx 直接失败。
- **第三方服务说明**：`provider="custom"` 会把**宿主人格**（`bot.nickname` / `personality.personality` /
  `personality.reply_style`，可用 `use_persona=false` 关闭）与**本次查询到的额度数据**
  （各指标名称与数值）发送到 `api_base_url`；**不会**发送聊天记录、群号、QQ 号或历史采样数据。
  请自行确认该服务可信。
- 人格注入：读取宿主 `bot.nickname`、`personality.personality`、`personality.reply_style`，
  最终提示词 = `人格` + `llm_prompt` + `查询数据`。
- 调用失败会自动回退为模板文本。

### 7.8 `[command]` 饭卡指令

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用 `/饭卡` 指令 |
| `default_query_keys` | str | `""` | 不带指标参数时默认查询的指标 id；留空=全部 |
| `notify_no_permission` | bool | `true` | 无权限时是否回复提示 |
| `deny_message` | str | `"你没有权限使用该命令"` | 无权限时的提示内容；留空则静默 |
| `send_mode` | str | `"image"` | `template` / `llm` / `image` |

### 7.9 权限（两组独立）

`[llm_tool_permission]`（LLM 工具）与 `[command_permission]`（`/饭卡` 指令）字段完全一致、互相独立：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `permission_mode` | str | `"whitelist"` | `whitelist`=名单外一律不可用；`blacklist`=名单外一律可用 |
| `whitelist_groups` | list[str] | `[]` | 白名单制度下允许的群号 |
| `whitelist_users` | list[str] | `[]` | 命中的 QQ 号始终可用（仍受放行指标约束） |
| `blacklist_groups` | list[str] | `[]` | 命中的群在任何制度下都不可用 |
| `blacklist_users` | list[str] | `[]` | 命中的 QQ 号始终不可用（最高优先级） |
| `allow_keys` | list[str] | `[]` | 全局默认可查询的指标 id；留空=全部放行 |
| `scope_overrides` | list[str] | `[]` | 每对话覆盖，格式 `type=群聊;id=123456;keys=minimax.interval_remaining` |

**判定顺序**：

1. 本地调试终端（`is_local_operator`，仅指令有该身份）始终放行；
2. 用户黑名单命中 → 始终拒绝；
3. 用户白名单命中 → 放行名单层（仍受放行指标约束）；
4. 群黑名单命中 → 拒绝；
5. `blacklist` 制度 → 未命中黑名单即放行；`whitelist` 制度 → 群聊需群号在白名单，
   私聊必须命中用户白名单；
6. 名单通过后解析放行指标集合：命中该对话的 `scope_overrides` → 用其集合；否则用 `allow_keys`。

`scope_overrides` 的 `keys` 支持 `k1,k2`、`*`（全部放行）或留空（全部禁止）。

### 7.10 `[render]` 图片渲染

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `template_name` | str | `"simple"` | 图片模板：`simple` / `dark` / `card`，或用户模板名 |
| `show_footer` | bool | `true` | 是否显示图片底部页脚 |
| `footer_text` | str | `"由麦麦饭卡插件渲染"` | 页脚文案；关闭「显示页脚」或此处置空则不显示页脚 |
| `image_format` | str | `"png"` | `png` 或 `jpeg` |
| `jpeg_quality` | int | `90` | JPEG 质量（1~100） |
| `resolution_scale` | float | `2.0` | 分辨率等级（1.0~4.0） |
| `first_round_timeout_ms` | int | `15000` | 第一轮渲染超时（毫秒） |
| `second_round_timeout_ms` | int | `25000` | 第二轮（回退轮）渲染超时（毫秒） |
| `viewport_width` / `viewport_height` | int | `1000` / `600` | 第一轮视口 |
| `fallback_viewport_width` / `fallback_viewport_height` | int | `900` / `1200` | 兜底视口 |
| `allow_network` | bool | `true` | 是否允许渲染页面访问外部网络（加载字体 CDN 需要） |
| `font_services` | list[str] | 内地 + 海外两组 | 字体服务组，每条 `name=;css=;static=` |

**自定义图片模板（可添加，无需改代码）**：往插件数据目录
`data/plugins/cssqy.mai-meal-card/templates/` 放一个 `.html`（根节点带 `id="meal-card"`，
用 `{{占位符}}` 引用数据），再到 `template_name` 填该文件名即可。

### 7.11 `[limits]` 请求上限

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `request_timeout_seconds` | float | `15.0` | 单次查询请求超时（秒） |
| `max_request_retries` | int | `2` | 查询请求失败后的重试次数（0~10） |

### 7.12 占位符（文本模板与图片模板共用）

| 占位符 | 含义 |
|---|---|
| `{date}` | 报告生成时间 |
| `{count}` | 指标数量 |
| `{items}` | 全部指标的多行文本区块（**多平台时按 `【平台名】` 分组**，单平台不加分组标题） |
| `{unavailable}` | 本次失败的指标清单（多平台时同样按平台分组） |
| `{platforms}` | 平台清单（`{platform}` 为其别名，取第一个平台） |
| `{<平台标识>.<指标id>}` | 单个指标的展示值 |
| `{<平台标识>.<指标id>_raw}` | 单个指标的纯数值 |
| `{<平台标识>.<指标id>_name}` | 单个指标的显示名称 |
| `{<平台标识>.<指标id>_progress}` | 单个指标的进度百分比 |
| `{<平台标识>.<指标id>_error}` | 单个指标的错误信息（成功时为空） |

- 只有一个平台时，额外提供不带前缀的简写 `{指标id}`。
- 文本模板用**单花括号** `{...}`；图片模板用**双花括号** `{{...}}`，
  另有 `{{items_html}}`、`{{title}}`、`{{footer}}`、`{{font_css}}`、`{{font_static}}`。
- **`{{items_html}}`**：多平台时按平台分组渲染为 `.platform-group` 块（带平台名标题
  `.platform-title` 与左侧色条，样式由模板 CSS 提供）；单平台时为扁平的 `<br>` 列表。
  自定义图片模板可自行给 `.platform-group` / `.platform-title` / `.platform-items` 写样式。
- **`{{title}}`**：报告标题，取宿主机器人昵称拼成「**{机器人昵称}的饭卡**」
  （读取宿主 `bot.nickname`；读不到时回退「麦麦饭卡」）。
- **`{{footer}}`**：图片页脚文案，取 `[render].footer_text`；关闭 `[render].show_footer`
  或文案留空时，模板里的页脚整行会被移除（不留空白）。自定义模板请把页脚写成
  `<div class="footer">{{footer}}</div>`，否则该开关不生效。
- 未知占位符会被置空并记一条 warning。
- **图片不展示「原始响应」**：成功的 `raw` 指标（原始响应摘要）不会出现在图片里；
  失败指标照常保留，便于在图上看到失败原因。文本模板与 LLM 工具结果同样默认剔除成功的 `raw`
  （可用 `[display].show_raw` 打开），且 `{count}` 与 `{items}` 的口径都基于过滤后的指标。

### 7.14 `[history]` 记录与统计

配置页「记录与统计」标签页。它同时是 WebUI「麦麦饭卡」页面的数据来源。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否按检查周期自动采样并记录历史 |
| `interval_seconds` | int | `1800` | 检查周期（秒），**也是曲线颗粒度**；范围 30 ~ 86400 |
| `retention_mode` | str | `"count"` | 保留方式：`count` 按检查次数 / `days` 按天数 / `size` 按文件大小 |
| `retention_count` | int | `2880` | 仅 `count` 生效：保留最近多少条采样（2880 ≈ 30 分钟一次约 60 天） |
| `retention_days` | int | `30` | 仅 `days` 生效：保留最近多少天 |
| `retention_size_mb` | float | `5.0` | 仅 `size` 生效：历史文件体积上限（MB），范围 0.1 ~ 200 |
| `chart_metrics` | list[str] | `[]` | WebUI 趋势曲线**同时追踪的指标 id**（最多 6 条；支持简写） |

- 三种保留方式**只生效选中的那一种**；裁剪从最旧的采样开始丢弃，且**至少保留 1 条**。
- `chart_metrics` 决定 WebUI 页面画哪几条曲线（每条一张独立的平滑曲线图，最多 6 条；
  超出部分忽略并记一条 warning）。填指标 id 后点「+」逐条添加，可随时增删；
  支持不带平台前缀的简写（如 `interval_remaining`）。
- 数据文件位于插件持久化目录：`data/plugins/cssqy.mai-meal-card/history.json`，
  采用「临时文件 + 原子替换」写入；文件损坏只会按空历史降级并记一条 warning，不影响插件运行。
- 采样写入时机：插件加载后立即一次，之后每 `interval_seconds` 一次；改动配置会立刻重算并重采一次；
  也可在 WebUI 页面点「立即采样」手动补一条。
- 单次采样有 8 秒总超时（与 WebUI 网关 10 秒预算对齐），超时只跳过本次并记 warning。

### 7.15 开箱默认值

| 分节 | 默认状态 | 说明 |
|---|---|---|
| `[plugin]` | `enabled = true`，三个数量均为 1 | 加载即生效 |
| `[platform1]` | `minimax` + `cn` + 空密钥 | **需要先填订阅密钥** |
| `[broadcast1]` / `[alert1]` | `enabled = false` | 默认关闭，避免未经确认就发消息 |
| `[display]` | `show_raw = false` | 默认不展示原始响应（排查时可开） |
| `[history]` | `enabled = true`、周期 1800 秒、按次数保留 2880 条 | 加载即开始记录，供 WebUI 曲线使用 |
| `[command]` | `enabled = true`、`send_mode = "image"` | `/饭卡` 可用 |
| 两组权限 | `permission_mode = "whitelist"`、放行指标留空 | **默认收紧**：只有名单内的对话可用 |

> 安全提示：默认两组权限都是 `whitelist`，即**只有把群号填进 `whitelist_groups`** 才能用。
> 想放开可改成 `blacklist` 并把名单留空；要限制能查什么就把指标 id 填进 `allow_keys`。

## 8. 已知限制

- 插件只做「HTTP 查询 + 解析 + 展示」，不直连数据库、不修改主程序。
- **MiniMax 的用量接口响应字段未在官方文档中承诺**：适配器采用防御式解析，
  字段缺失只跳过对应指标，解析失败会在日志中打印截断后的原始响应（并保留 `raw` 指标），
  **不会静默填 0**。
- 额度受「5 小时滚动窗口 + 周窗口」控制，未用完不结转；文本/图像/语音共享同一份额度。
- 订阅密钥与按量付费 API Key **不通用**；区域选错会返回 401。
- **DeepSeek 适配只读余额**：仅查询账户余额（`GET /user/balance`），不产生任何费用；
  余额不足时响应里 `is_available=false`（对应指标「账户可用状态」）。
- **原始响应默认不外发**：成功的 `raw`（平台响应体摘要）默认不进文本模板、LLM 转述与 LLM 工具结果，
  图片渲染始终不展示；`raw` 出站前还会按键名做启发式脱敏（含 `key` / `token` / `secret` /
  `password` / `credential` / `authorization` / `cookie` / `session` 的字段值替换为 `***`）。
  需要排查接口异常时可用 `[display].show_raw` 临时打开。
- **槽位数量改动需重载**：数量在插件加载时确定，改大后需「禁用再启用」插件（或重启）才生成新槽位。
- 播报漏过的时刻不补发、执行失败不重试（仅记日志）；警报在每个间隔检查点、条件为真时投递
  （不额外去重）。
- 图片渲染依赖宿主 `render.html2png`；渲染失败会自动回退为文本版本并记一条 error 日志；
  单次渲染有 45 秒总预算。
- **WebUI 页面的「当前值」取自最近一次采样**（不是点击瞬间的实时请求）：宿主网关单次调用限 10 秒，
  实时查询多平台容易超时，因此统一走历史采样；需要最新数据时点页面上的「立即采样」。
- 历史记录依赖宿主提供的插件数据目录；宿主未提供时页面会显示「记录存储不可用」，其余功能不受影响。

## 9. 常见问题

- **配置页看不到 `platform2`？** 先把 `[plugin].platform_count` 改成 2，保存后「禁用再启用」插件。
- **提示「未填写 MiniMax 订阅密钥」？** 去对应平台槽位的「订阅密钥」填 TokenPlan 订阅密钥。
- **返回 401？** 区域与套餐购买地区不一致，或用了按量付费的普通 API Key。
- **DeepSeek 返回 401？** 确认「平台类型」选的是 `deepseek`，且填的是开放平台 API Key（`sk-` 开头、未过期）。
- **条件里写 `interval_remaining` 不生效？** 多平台时指标 id 带前缀，请写 `minimax.interval_remaining`。
- **`/饭卡` 没反应？** 检查 `[command].enabled`，以及 `[command_permission]` 是否放行当前群/用户。
- **LLM 说没权限？** `[llm_tool_permission]` 默认白名单，把群号填进 `whitelist_groups`，
  或把 `permission_mode` 改成 `blacklist` 并留空名单。
- **只收到图片、没有文字？** 这是 `send_mode = "image"` 的预期行为。
- **侧边栏找不到「麦麦饭卡」页面？** 插件需先成功加载；页面在「插件扩展」分组下，
  若被隐藏可在插件配置页的「WebUI 扩展」区域重新显示。
- **WebUI 曲线是空的？** 检查 `[history].enabled` 是否为开启、平台密钥是否有效，
  以及「趋势曲线指标」是否已添加（留空时页面只显示「临时查看」曲线）；
  首次记录在插件加载后立即开始，也可点页面上的「立即采样」。
- **想换页面上的曲线？** 在「记录与统计 → 趋势曲线指标」里增删指标 id（最多 6 条），保存后刷新页面即可；
  页面上的「临时查看」输入框只影响它自己那张图，不改配置。
- **页面里的「当前值」像是旧的？** 它取自最近一次采样（默认每 30 分钟一次），
  点「立即采样」即可刷新到最新；实时查询不在页面里做是为了规避宿主 10 秒调用上限。
- **图片没发出来、只收到文字？** 说明渲染整体失败（字体服务不可用、浏览器不可用、超预算等），
  插件会记一条 error 日志并自动回退文字版本。
- **旧配置会丢吗？** 不会：`[platform]`/`[minimax]` 与旧 `[broadcast].tasks`/`[alerts].tasks`
  会在加载时自动迁移到槽位（迁移只做一次）。

## 10. 如何新增内置平台

1. 在 `plugins/mai_meal_card/platforms/` 下新建 `<平台>.py`，实现 `PlatformAdapter`：

   ```python
   from .base import MetricValue, KIND_PERCENT

   class MyPlatformAdapter:
       id = "myplatform"
       display_name = "我的平台"

       def is_configured(self, slot) -> tuple[bool, str]:
           ...  # 返回 (是否已配置, 未配置原因)

       def metric_ids(self) -> list[str]:
           return ["my_metric"]

       def describe(self, slot) -> str:
           return self.display_name

       async def fetch(self, slot, *, timeout: float, retries: int) -> list[MetricValue]:
           ...  # 返回指标列表；失败项用 ok=False 表达，不抛异常
   ```

2. 在 `platforms/__init__.py` 末尾注册：`register(MyPlatformAdapter())`。
3. 在 `config.PlatformSlotConfig.adapter` 的 `Literal` 中加入该平台 id，
   并为该平台新增所需字段（参考 `minimax` 与 `custom` 两组字段）。

其余模块（查询引擎、权限、调度、模板、渲染）无需改动。

## 11. 开发说明

| 文件 | 职责 |
|---|---|
| `plugin.py` | 插件入口：生命周期、`@Tool`、`@Command`、配置归一化与配置页标签页 |
| `config.py` | 配置节 + 槽位类生成（数量控制）+ 任务/覆盖解析 |
| `platforms/base.py` | 适配器接口与 `MetricValue` 数据类 |
| `platforms/minimax.py` | MiniMax TokenPlan 适配器 |
| `platforms/deepseek.py` | DeepSeek 开放平台适配器（账户余额） |
| `platforms/custom.py` | 自定义平台适配器 |
| `platforms/__init__.py` | 平台注册表 |
| `history.py` | 历史采样存储（三种保留策略）与平滑曲线生成 |
| `webui.json` | WebUI「麦麦饭卡」页面的声明式定义（宿主 WebUI 协议 v2，`schema_version: 2`） |
| `query_engine.py` | 多平台调度、指标过滤与展示格式化 |
| `conditions.py` | `and/or/not` + 比较运算的表达式解析与求值 |
| `permissions.py` | 两组查询权限的名单与放行指标判定 |
| `text_report.py` | 文本模板渲染与占位符构造 |
| `llm_rewrite.py` | LLM 风格化转述（宿主模型组 / 自定义兼容服务） |
| `templates.py` | 图片 HTML 模板加载（内置 + 用户新增）与占位符填充 |
| `renderer.py` | 渲染管线：字体服务轮换、两轮超时、PNG/JPEG、失败回退 |
| `delivery.py` | 目标解析（群/用户 → stream_id）与发送 |
| `scheduler.py` | 定时播报与警报的调度循环 |
| `templates/*.html` | 内置图片模板 |

代码检查：`python -m py_compile plugins/mai_meal_card/*.py plugins/mai_meal_card/platforms/*.py`
（ruff 可用时：`uv run ruff check plugins/mai_meal_card`）。

## 12. 在 MaiBot 中启用与测试

1. 把整个 `mai_meal_card/` 目录放到 MaiBot 的 `plugins/` 下（保持目录名不变）。
2. 启动 / 重启 MaiBot；插件在插件运行时（Runner）进程中被自动发现并加载。
3. 首次加载会按配置模型自动生成 `plugins/mai_meal_card/config.toml`；之后修改该文件即热更新。
4. 在配置页「平台」标签页选择平台类型并填写密钥（minimax：区域 + TokenPlan 订阅密钥；deepseek：API Key）。
5. 群里发 `/饭卡` 验证；让 LLM 调用 `query_meal_card` 验证工具。
6. 把 `[plugin].broadcast_count` / `alert_count` 改成需要的数量 → 保存 → 重载插件，
   再到对应标签页填写任务，验证到点播报与命中条件警报。
7. 打开侧边栏「插件扩展 → 麦麦饭卡」页面，确认概览卡片有数据、趋势曲线能画出平滑曲线
   （可先在「记录与统计」标签页把检查周期调小、或点「立即采样」攒几个点）。

## 13. 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。
