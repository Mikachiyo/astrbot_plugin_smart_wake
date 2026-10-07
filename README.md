# astrbot_plugin_smart_wake — 智能主动唤醒

群聊主动唤醒插件：AstrBot 原生群聊需要 @/唤醒词才唤醒，本插件监听未唤醒的群消息，用「关键词抽签 → Jev 模型判定」两级漏斗决定是否让 bot 主动参与聊天。Jev 只做判断，回复内容仍由 AstrBot 主 LLM 生成（人格、记忆、其他插件全不受影响）。

## 功能

- **两级抽签唤醒**：话题关键词命中后按分值掷骰（第一级），通过后交给 Jev 模型结合上下文判定（第二级：回复价值 × 插话自然度 × 热度系数 × 精力系数）
- **话题评分表**：内置 9 类预设话题（技术问答/礼仪问候/节日祝福等），可在 dashboard 增删改
- **必唤醒词**：命中直接唤醒，不走任何抽签
- **节日观望**：节日祝福命中后先观望 10 分钟，有人复读就跟读，没人接话才补一句祝福
- **复读跟读**：整合自 Zhalslar/astrbot_plugin_reread（MIT），文本/图片/表情连发触发跟读
- **conversation 态**：唤醒后进入会话状态，Jev 防抖判定何时结束对话，超时按 bot 回复字数走曲线
- **闭嘴状态机**：被群友要求闭嘴（经 Jev 确认）后进入 muted，压住一切唤醒包括原生 @，群冷了或被允许说话时自动解除
- **精力系统**：回复消耗精力（越累代价越高），随时间/消息恢复，精力低时更不爱说话
- **决策日志**：所有判定（骰子值、门槛、Jev 原始返回）保留最近 200 条，dashboard 可查看

## 配置

### 1. 获取 Jev API Key

Jev 是 TypeSafe AI 的 "System One" 单步决策模型（不兼容 OpenAI chat 格式）：

- **官方直连**：到 TypeSafe AI 官网申请 API Key，base_url 填 `https://api.typesafe.ai`
- **OpenRouter 中转**：在 [OpenRouter](https://openrouter.ai) 充值后创建 API Key，通道选 `openrouter`，base_url 填 `https://openrouter.ai`（模型自动映射为 `typesafe/jev-1.13`）

支持配置多个 Key 轮询，失败的 Key 自动冷却（默认 300 秒）。

### 2. 填写配置

- **简单配置**：AstrBot WebUI → 插件管理 → 智能主动唤醒 → 配置（通道/地址/模型/Key/各项阈值）
- **复杂配置**（话题表、超时曲线、精力消耗曲线、决策日志、群状态总览）：dashboard 插件页面 `pages/admin/index.html`

### 3. 关键配置说明

| 配置 | 默认 | 说明 |
|---|---|---|
| fallback_probability | 10% | 无话题命中时的兜底放行概率 |
| hot/cold_threshold | 20/5 | 5 分钟消息数分档热度 |
| mute_threshold | 50% | 闭嘴判定阈值（故意放低） |
| energy_gamma | 2 | 精力系数 = (E/上限)^γ |
| end_threshold | 60% | 对话结束判定置信度 |

## 原理速览

```
群消息 → 过滤(自己/黑白名单/指令) → 缓冲+热度
       → muted? → 闭嘴解除判定
       → conversation? → 结束判定缓冲
       → 复读检测(触发则跟读)
       → 话题匹配 → 一级抽签(分值×10%)
       → Jev 判定(response_value × natural × 热度 × 精力) → 二级抽签
       → event.is_at_or_wake_command = True → 主 LLM 正常回复
```

状态（精力/冷却/muted/会话）持久化在 AstrBot 数据目录 `plugin_data/astrbot_plugin_smart_wake/state.json`，重启不丢。

节日延迟祝福通过 `Context.get_event_queue()` → 官方 `EventBus` → 完整 `PipelineScheduler` 处理，使用基于官方 `CronMessageEvent` 的主动事件，不再直调模型或手工覆盖历史。保留触发时完整 UMO（包括 unique_session），provider、人设、历史及请求/响应/发送钩子由内核处理；标注 `_active_trigger` / `active_reply_triggered`，发送者为定时任务而非真实群友，不进入 smart_wake 的用户 conversation。需要 AstrBot 4.28.0 对应接口以及平台支持主动发送；事件处理最多等待 300 秒，失败/无回复不写节日成功冷却，不回退直调。节日事件使用非流式回复，以便完整执行结果装饰与发送后钩子。

`reread_suppression_seconds` 默认 **0**，保留旧的“最近一次已处理指纹持续抑制，直到其他指纹触发”语义；正数表示抑制秒数，到期后从新消息重新累计阈值。复读窗口仍按文本/图片/表情分别计数，其他类型插入不清空已有类型窗口；连续同一发送者会重计，阈值内必须全部为不同发送者，多段或无法识别的消息不参与。

精力上限变化时，已有群精力裁剪到当前上限，新群默认值同步；上调上限不会直接补满旧群。卸载会取消并等待插件任务（含节日管线与会话超时），取消防抖保存计时器后最终保存状态。

## 依赖

仅 httpx（AstrBot 自带），无新增依赖。关键词匹配为子串匹配，不用 jieba。
