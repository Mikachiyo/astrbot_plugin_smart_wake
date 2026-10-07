"""智能主动唤醒 — 关键词抽签 + Jev 模型判定的群聊主动唤醒插件。

监听未唤醒的群消息，两级漏斗决定是否让 bot 主动参与聊天；
Jev 只做判断，回复内容仍由 AstrBot 主 LLM 生成。
"""

import asyncio
import random
import time
from collections import deque

from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType

from .core.curves import interp, normalize_points
from .core.jev_client import JevClient, JevError
from .core.matcher import DEFAULT_TOPICS, first_level_draw, hit_must_wake, match_topics
from .core.prompts import (
    build_end_questions,
    build_shutup_questions,
    build_unmute_questions,
    build_wake_questions,
)
from .core.reread import make_fingerprint
from .core.state import StateStore

# WebUI 标量配置项（_conf_schema.json 与 dashboard 共用这份清单）
SCALAR_KEYS = [
    "jev_channel", "jev_base_url", "jev_model", "jev_api_keys", "jev_timeout",
    "key_cooldown_seconds", "bot_name", "bot_persona",
    "group_whitelist", "group_blacklist",
    "fallback_probability", "hot_threshold", "cold_threshold",
    "heat_hot_casual", "heat_hot_question", "heat_cold_casual", "heat_cold_question",
    "festival_grace_minutes", "festival_cooldown_hours",
    "reread_enabled", "reread_text_threshold", "reread_image_threshold",
    "reread_face_threshold", "reread_follow_probability", "reread_suppression_seconds",
    "mute_threshold", "unmute_threshold", "cool_check_msgs", "cool_minutes",
    "energy_enabled", "energy_max", "energy_gamma",
    "energy_recover_by_msg", "energy_recover_msgs", "energy_recover_per_msgs",
    "energy_recover_by_time", "energy_recover_per_min",
    "end_threshold", "batch_max", "context_messages",
    "shutup_words", "unmute_words", "must_wake_words", "debug_log",
    "timeout_curve_x_max", "timeout_curve_y_max", "energy_curve_x_max", "energy_curve_y_max",
    "bot_names", "ignore_at_other",
    "prompt_wake_value", "prompt_wake_natural", "prompt_end_conversation", "prompt_skip_reply",
    "prompt_shutup", "prompt_unmute",
]

DEFAULT_SHUTUP_WORDS = ["闭嘴", "别说话", "别说了", "安静", "别插嘴", "烦死", "吵死", "别回", "不用你"]
DEFAULT_UNMUTE_WORDS = ["可以说话了", "不用闭嘴了", "解除闭嘴", "出来吧", "说话吧"]


@register(
    name="astrbot_plugin_smart_wake",
    desc="智能主动唤醒：关键词抽签 + Jev 模型判定，让 bot 主动参与群聊",
    version="v1.0.0",
    author="Mikachiyo",
)
class SmartWakePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        # 私有数据备份路径（双保险，防止内核清理未登记字段）
        from pathlib import Path
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        self._curves_backup_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_smart_wake" / "curves_and_topics.json"
        self._load_curves_backup()

        self.jev = JevClient(config)
        self.store = StateStore(energy_default=float(config.get("energy_max", 100) or 100))
        self._sync_energy_max()
        self.logs: deque[dict] = deque(maxlen=200)  # 决策日志
        self._closing = False
        self._tasks: set[asyncio.Task] = set()
        self._register_web_apis()
        # 按时间恢复精力的后台循环
        self._spawn(self._energy_recover_loop())
        logger.info("[smart_wake] 智能主动唤醒插件已加载")

    def _load_curves_backup(self) -> None:
        """从插件私有数据目录恢复曲线与话题表，提供双保险。"""
        import json
        backup_data = {}
        if self._curves_backup_path.exists():
            try:
                backup_data = json.loads(self._curves_backup_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[smart_wake] 读取本地曲线备份失败: {e}")

        # 话题表
        if not isinstance(self.config.get("topics"), list) or not self.config.get("topics"):
            self.config["topics"] = backup_data.get("topics") or DEFAULT_TOPICS
        # 超时曲线
        if not isinstance(self.config.get("timeout_curve"), list) or not self.config.get("timeout_curve"):
            self.config["timeout_curve"] = backup_data.get("timeout_curve") or [[0, 10], [300, 60]]
        # 精力消耗曲线
        if not isinstance(self.config.get("energy_cost_curve"), list) or not self.config.get("energy_cost_curve"):
            self.config["energy_cost_curve"] = backup_data.get("energy_cost_curve") or [[100, 10], [20, 40]]
        # 曲线坐标上限
        for k, default_val in [
            ("timeout_curve_x_max", 500),
            ("timeout_curve_y_max", 90),
            ("energy_curve_x_max", 100),
            ("energy_curve_y_max", 60),
        ]:
            if not self.config.get(k):
                self.config[k] = backup_data.get(k, default_val)
        # 词汇
        if not self.config.get("shutup_words"):
            self.config["shutup_words"] = DEFAULT_SHUTUP_WORDS
        if not self.config.get("unmute_words"):
            self.config["unmute_words"] = DEFAULT_UNMUTE_WORDS

        # 启动时同步写一次私有备份
        self._save_curves_backup()

    def _save_curves_backup(self) -> None:
        """将当前曲线与话题数据同步写入插件私有数据目录，确保永不丢失。"""
        import json
        try:
            self._curves_backup_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "topics": self.config.get("topics"),
                "timeout_curve": self.config.get("timeout_curve"),
                "energy_cost_curve": self.config.get("energy_cost_curve"),
                "timeout_curve_x_max": self.config.get("timeout_curve_x_max"),
                "timeout_curve_y_max": self.config.get("timeout_curve_y_max"),
                "energy_curve_x_max": self.config.get("energy_curve_x_max"),
                "energy_curve_y_max": self.config.get("energy_curve_y_max"),
            }
            tmp = self._curves_backup_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._curves_backup_path)
        except Exception as e:
            logger.warning(f"[smart_wake] 保存本地曲线备份失败: {e}")

    # ---------- 工具 ----------

    def _spawn(self, coro) -> asyncio.Task | None:
        """统一追踪插件任务，卸载后不再创建。"""
        if self._closing:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    def _get_bot_name_str(self) -> str:
        names = [str(x).strip() for x in (self._cfg("bot_names") or []) if str(x).strip()]
        if not names:
            single = (self._cfg("bot_name") or "").strip()
            names = [single] if single else ["bot"]
        if len(names) == 1:
            return names[0]
        return f"{names[0]}（别名/昵称: {'、'.join(names[1:])}）"

    def _log(self, gid: str, text: str, result: str, **extra) -> None:
        entry = {"ts": time.time(), "group": gid, "msg": (text or "")[:80],
                 "result": result, **extra}
        self.logs.append(entry)
        if self._cfg("debug_log", False):
            logger.info(f"[smart_wake] {result} | {entry}")

    def _group_allowed(self, gid: str, uid: str) -> bool:
        whitelist = [str(x) for x in (self._cfg("group_whitelist") or []) if str(x).strip()]
        blacklist = [str(x) for x in (self._cfg("group_blacklist") or []) if str(x).strip()]
        if gid in blacklist or uid in blacklist:
            return False
        if whitelist and gid not in whitelist:
            return False
        return True

    def _heat_level(self, count: int) -> str:
        if count >= int(self._cfg("hot_threshold", 20)):
            return "hot"
        if count <= int(self._cfg("cold_threshold", 5)):
            return "cold"
        return "warm"

    def _heat_factor(self, level: str, is_question: bool) -> float:
        suffix = "question" if is_question else "casual"
        if level == "hot":
            return float(self._cfg(f"heat_hot_{suffix}", 1.0 if is_question else 0.5))
        if level == "cold":
            return float(self._cfg(f"heat_cold_{suffix}", 1.0 if is_question else 1.3))
        return 1.0

    def _energy_factor(self, energy: float) -> float:
        emax = float(self._cfg("energy_max", 100) or 100)
        gamma = float(self._cfg("energy_gamma", 2.0) or 2.0)
        return (max(energy, 0.0) / emax) ** gamma

    # ---------- 群消息主入口 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99999)
    async def on_group_msg(self, event: AstrMessageEvent):
        try:
            await self._handle_group_msg(event)
        except Exception as e:
            # 插件任何异常不得打断消息 pipeline
            logger.error(f"[smart_wake] handler 异常: {e}", exc_info=True)

    async def _handle_group_msg(self, event: AstrMessageEvent):
        if event.get_extra("_smart_wake_festival"):
            event.pipeline_task = asyncio.current_task()
            gs = self.store.get(str(event.get_group_id()))
            if (self._closing or event.done.cancelled() or gs.muted
                    or not gs.festival_watch or not event.send_allowed()):
                event.stop_event()
            return
        if self._closing:
            return
        self._sync_energy_max()
        gid = event.get_group_id()
        if not gid or getattr(event.message_obj, "type", None) != MessageType.GROUP_MESSAGE:
            return
        uid = event.get_sender_id()
        if uid == event.get_self_id():
            return
        if not self._group_allowed(str(gid), str(uid)):
            return

        gs = self.store.get(str(gid))
        gs.umo = event.unified_msg_origin
        now = time.time()

        chain = event.get_messages()
        text = " ".join(s.text for s in chain if isinstance(s, Plain)).strip()
        # 指令前缀过滤
        wake_prefix = (self._cfg("wake_prefix") or "").strip()
        if text.startswith("/") or (wake_prefix and text.startswith(wake_prefix)):
            return

        nickname = ""
        try:
            nickname = event.message_obj.sender.nickname or str(uid)
        except Exception:
            nickname = str(uid)

        is_wake = bool(getattr(event, "is_at_or_wake_command", False))

        # 2. 记入环形缓冲；3. 热度计数（唤醒消息也更新）
        if text:
            gs.buffer.append({"nickname": nickname, "text": text, "is_bot": False, "ts": now})
        heat_count = gs.add_heat(now)
        # 精力按消息恢复
        if self._cfg("energy_recover_by_msg", True):
            gs.msg_counter += 1
            if gs.msg_counter >= int(self._cfg("energy_recover_msgs", 10)):
                gs.msg_counter = 0
                self._recover_energy(gs, float(self._cfg("energy_recover_per_msgs", 5)))

        # 4. muted 状态
        if gs.muted:
            await self._handle_muted(event, gs, text, is_wake, nickname, now)
            return

        # 艾特其他人/其他Bot检测：若启用了 ignore_at_other 且消息明确艾特了其他人
        has_at_other = False
        if self._cfg("ignore_at_other", True):
            self_id = str(event.get_self_id())
            from astrbot.api.message_components import At
            has_at_other = any(
                isinstance(s, At) and str(getattr(s, "qq", "")) not in (self_id, "all", "")
                for s in chain
            )

        # 5. 闭嘴候选词检测（无论是否在会话中，只要命中闭嘴词都直接进入闭嘴判定！）
        if text:
            shutup_hit = any(w and w in text for w in (self._cfg("shutup_words") or DEFAULT_SHUTUP_WORDS))
            if shutup_hit:
                await self._judge_shutup(event, is_wake, gs, text, nickname, now)
                return

        # 6. conversation 态检查（按用户独立隔离）
        u_conv = gs.conversations.get(str(uid))
        if u_conv and not is_wake:
            if has_at_other:
                self._log(str(gid), text, "会话中检测到艾特他人/其他Bot，跳过本次回复", target=str(uid))
                if u_conv.timeout_task:
                    u_conv.timeout_task.cancel()
                self._schedule_conversation_timeout(gs, u_conv, 30)
                return

            if not text:
                # 非文本消息（纯表情/图片等）：保留当前超时倒计时，不判定唤醒，防止变成僵尸会话！
                return

            # 确认是有效文本后，再取消旧的倒计时
            if u_conv.timeout_task:
                u_conv.timeout_task.cancel()
                u_conv.timeout_task = None

            await self._handle_conversation_msg(event, gs, u_conv, text, nickname, uid)
            return

        if has_at_other:
            self._log(str(gid), text, "检测到艾特他人/其他Bot，跳过唤醒判定")
            return

        # 唤醒消息：跳过唤醒评估，仅更新状态（缓冲/热度/复读窗口已在上面做）
        if is_wake:
            return

        if not text:
            # 非文本消息仍参与复读检测
            await self._try_reread(event, gs, chain, uid)
            return

        # 7. 复读检测：触发跟读则本条不再评估唤醒
        if await self._try_reread(event, gs, chain, uid):
            return

        # 必唤醒词：直接唤醒，不走抽签
        must = hit_must_wake(text, self._cfg("must_wake_words") or [])
        if must:
            self._log(str(gid), text, "必唤醒词唤醒", must_word=must)
            await self._wake(event, gs, uid)
            return

        # 7. 第一级抽签
        draw = first_level_draw(text, self._cfg("topics") or [], gs.topic_cooldowns,
                                int(self._cfg("fallback_probability", 10)), now)
        if not draw["passed"]:
            self._log(str(gid), text, "一级抽签未通过", hits=draw["hits"], fallback=draw["fallback"])
            return

        topic = draw["topic"]
        # 节日旁路：观望期内不直接问 Jev
        if topic and topic.get("key") == "festival_blessing":
            self._start_festival_watch(gs, text, event)
            self._log(str(gid), text, "节日话题→进入观望", topic=topic.get("name"))
            return

        # 8. Jev 唤醒判定 + 第二级抽签
        await self._judge_wake(event, gs, text, nickname, uid, topic, heat_count, draw)

    # ---------- 复读跟读 ----------

    async def _try_reread(self, event, gs, chain, uid) -> bool:
        if not self._cfg("reread_enabled", True):
            return False
        fp = make_fingerprint(chain)
        if not fp:
            return False
        kind, fingerprint = fp
        threshold = int(self._cfg(f"reread_{kind}_threshold", {"text": 4, "image": 3, "face": 2}[kind]))
        if not gs.reread.process(
            kind, fingerprint, str(uid), threshold,
            suppression_seconds=max(0.0, float(self._cfg("reread_suppression_seconds", 0) or 0)),
        ):
            return False
        # 触发 → 掷骰跟读
        follow_p = int(self._cfg("reread_follow_probability", 80)) / 100
        roll = random.random()
        self._log(gs.gid, fingerprint, f"复读触发 follow_roll={roll:.2f} p={follow_p:.2f}")
        if roll >= follow_p:
            return True  # 已触发但不跟读，也不再评估唤醒
        try:
            await event.send(MessageChain(chain=list(chain)))
        except Exception as e:
            logger.warning(f"[smart_wake] 复读跟读发送失败: {e}")
            return True
        # 跟读成功后对内容跑话题匹配 → 命中节日祝福写 1 天冷却
        if kind == "text":
            content = fingerprint[len("text:"):]
            hits = match_topics(content, self._cfg("topics") or [])
            if any(t.get("key") == "festival_blessing" for t in hits):
                cd = float(self._cfg("festival_cooldown_hours", 24)) * 3600
                gs.topic_cooldowns["festival_blessing"] = time.time() + cd
                self.store.save_debounced()
                # 节日观望期内跟读成功 → 结束观望
                gs.festival_watch = None
        return True

    # ---------- 唤醒判定（第二级） ----------

    async def _judge_wake(self, event, gs, text, nickname, uid, topic, heat_count, draw):
        heat_level = self._heat_level(heat_count)
        context_n = int(self._cfg("context_messages", 15))
        recent = [f"{m['nickname']}: {m['text']}" for m in list(gs.buffer)[-context_n:]]
        bot_name = self._get_bot_name_str()
        if topic:
            hint = f"前置关键词分析：疑似 {topic.get('name')}（该话题分值 {topic.get('score')}/10），仅供参考，请结合语义验证"
        else:
            hint = "前置关键词分析：无命中话题（兜底抽签通过），请结合语义判断"
        state = {
            "bot_name": bot_name,
            "bot_persona": self._cfg("bot_persona") or "",
            "chat_heat": heat_level,
            "prefilter_hint": hint,
            "recent_messages": recent + [f"→ 当前消息: {nickname}: {text}"],
        }
        questions = build_wake_questions(
            self._cfg("topics") or [],
            topic,
            val_instructions=self._cfg("prompt_wake_value") or "",
            nat_instructions=self._cfg("prompt_wake_natural") or "",
        )
        try:
            resp = await self.jev.decide(state, questions)
        except Exception as e:
            # Jev 失败按不唤醒处理
            self._log(gs.gid, text, f"Jev失败不唤醒: {e}", topic=(topic or {}).get("key"))
            return
        answers = resp.get("answers") or {}
        try:
            value01 = float((answers.get("response_value") or {}).get("score", 0)) / 4  # 5 档归一化
            natural = float((answers.get("natural_to_join") or {}).get("noul", 0))
        except (TypeError, ValueError):
            self._log(gs.gid, text, "Jev响应解析失败", answers=answers)
            return
        is_question = bool((topic or {}).get("is_question", False))
        hf = self._heat_factor(heat_level, is_question)
        ef = self._energy_factor(gs.energy) if self._cfg("energy_enabled", True) else 1.0
        p = value01 * natural * hf * ef
        roll = random.random()
        passed = roll < p
        self._log(gs.gid, text, "二级抽签" + ("通过→唤醒" if passed else "未通过"),
                  topic=(topic or {}).get("key"), value01=round(value01, 3), natural=round(natural, 3),
                  heat=heat_level, heat_factor=hf, energy_factor=round(ef, 3),
                  p=round(p, 4), roll=round(roll, 4), jev=answers)
        if not passed:
            return
        # 9. 唤醒
        if topic:
            cd = float(topic.get("cooldown_seconds") or 0)
            if cd > 0:
                gs.topic_cooldowns[topic["key"]] = time.time() + cd
        await self._wake(event, gs, uid)

    async def _wake(self, event, gs, uid) -> None:
        """放行事件走主 LLM，并进入该用户的 conversation 态、扣精力。"""
        u_conv = gs.get_or_create_conv(str(uid), gs.umo)
        u_conv.waiting_llm = True
        u_conv.conv_buffer = []
        self._consume_energy(gs)
        self.store.save_debounced()
        event.is_at_or_wake_command = True

    # ---------- 精力 ----------

    def _consume_energy(self, gs) -> None:
        if not self._cfg("energy_enabled", True):
            return
        cost = interp(self._cfg("energy_cost_curve") or [[100, 10], [20, 40]], gs.energy)
        gs.energy = max(0.0, gs.energy - cost)
        self._log(gs.gid, "", f"精力消耗 -{cost:.1f} → {gs.energy:.1f}")

    def _sync_energy_max(self) -> float:
        emax = max(0.0, float(self._cfg("energy_max", 100) or 100))
        changed = self.store.set_energy_max(emax)
        if changed:
            self.store.save_debounced()
        return emax

    def _recover_energy(self, gs, base_amount: float) -> None:
        emax = self._sync_energy_max()
        if not self._cfg("energy_enabled", True) or emax <= 0:
            return
        rate = base_amount * (gs.energy / emax)  # 越累恢复越慢
        gs.energy = max(0.0, min(emax, gs.energy + rate))

    async def _energy_recover_loop(self) -> None:
        """按时间恢复精力：每分钟一次。"""
        while True:
            try:
                await asyncio.sleep(60)
                self._sync_energy_max()
                if self._cfg("energy_recover_by_time", True):
                    amount = float(self._cfg("energy_recover_per_min", 3))
                    for gs in self.store.groups.values():
                        self._recover_energy(gs, amount)
                    self.store.save_debounced()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[smart_wake] 精力恢复循环异常: {e}")

    # ---------- 节日观望 ----------

    def _start_festival_watch(self, gs, text: str, event: AstrMessageEvent) -> None:
        if gs.festival_watch:
            return  # 同一观望期及其生成期间只触发一次
        grace = float(self._cfg("festival_grace_minutes", 10)) * 60
        watch = {"text": text, "deadline": time.time() + grace,
                 "umo": event.unified_msg_origin, "self_id": event.get_self_id()}
        gs.festival_watch = watch
        self._spawn(self._festival_watch_task(gs.gid, watch))

    async def _festival_watch_task(self, gid: str, watch: dict) -> None:
        gs = self.store.get(gid)
        try:
            delay = max(1.0, watch["deadline"] - time.time())
            await asyncio.sleep(delay)
            if gs.festival_watch is not watch or gs.muted:
                return
            sent = await self._delayed_reply(
                gid, watch["umo"], self_id=watch["self_id"],
                prompt=(
                    f"群里刚才在聊节日祝福（原话：{watch['text']}），"
                    f"你观望了一会儿没插话。现在请以群友身份自然地补一句节日祝福或回应，简短即可。"
                ),
            )
            if not sent:
                self._log(gid, watch["text"], "节日主动事件未成功发送，不写冷却")
                return
            cd = float(self._cfg("festival_cooldown_hours", 24)) * 3600
            gs.topic_cooldowns["festival_blessing"] = time.time() + cd
            self.store.save_debounced()
            self._log(gid, watch["text"], "节日观望期满→官方管线已发送祝福")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[smart_wake] 节日观望任务异常: {e}")
        finally:
            if gs.festival_watch is watch:
                gs.festival_watch = None

    # ---------- 节日主动事件（官方完整 pipeline） ----------

    async def _delayed_reply(self, gid: str, umo: str, prompt: str, self_id: str) -> bool:
        from .core.festival import FestivalMessageEvent

        gs = self.store.get(gid)
        watch = gs.festival_watch
        event = FestivalMessageEvent(
            self.context, umo=umo, gid=gid, self_id=self_id, prompt=prompt
        )
        event.send_allowed = lambda: (
            not self._closing and not gs.muted and gs.festival_watch is watch
        )
        try:
            await self.context.get_event_queue().put(event)
            # 包括排队、模型和发送；停止/无结果/发送失败都不能写成功冷却。
            sent = await asyncio.wait_for(event.done, timeout=300)
            if sent and event.delivered_texts:
                text = "\n".join(event.delivered_texts)
                gs.bot_replies.append(text)
                gs.buffer.append({"nickname": f"{self._cfg('bot_name') or 'bot'}(bot自己)",
                                  "text": text, "is_bot": True, "ts": time.time()})
            return sent
        finally:
            event.stop_event()
            if not event.done.done():
                event.done.cancel()
            task = event.pipeline_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    # ---------- conversation 态（按用户独立隔离） ----------

    async def _handle_conversation_msg(self, event: AstrMessageEvent, gs, u_conv, text: str, nickname: str, uid: str) -> None:
        """在事件生命周期内直接由 Jev 研判是结束对话、跳过回复、还是继续对话放行官方原生主管线。"""
        context_n = int(self._cfg("context_messages", 15))
        recent = [f"{m['nickname']}: {m['text']}" for m in list(gs.buffer)[-context_n:]]
        state = {
            "bot_name": self._get_bot_name_str(),
            "bot_persona": self._cfg("bot_persona") or "",
            "target_user_id": u_conv.uid,
            "recent_messages": recent,
            "new_messages": [f"{nickname}: {text}"],
        }
        hits = match_topics(text, self._cfg("topics") or [])
        hint = f"命中话题: {'/'.join(t.get('name') for t in hits)}" if hits else "无话题命中"
        questions = build_end_questions(
            hint,
            end_instructions=self._cfg("prompt_end_conversation") or "",
            skip_instructions=self._cfg("prompt_skip_reply") or "",
        )
        try:
            resp = await self.jev.decide(state, questions)
        except Exception as e:
            self._log(gs.gid, text, f"用户({u_conv.uid})会话判定Jev失败，直接放行唤醒: {e}")
            await self._wake(event, gs, uid)
            return

        answers = resp.get("answers") or {}
        conf_end = float((answers.get("end_conversation") or {}).get("noul", 0) or 0)
        conf_skip = float((answers.get("skip_reply") or {}).get("noul", 0) or 0)
        threshold = int(self._cfg("end_threshold", 60)) / 100

        # 分支 1：用户明确意图结束/道别 → 退出会话，不唤醒
        if conf_end >= threshold:
            self._log(gs.gid, text, f"用户({u_conv.uid})结束对话 conf_end={conf_end:.2f}")
            self._exit_conversation(gs, u_conv)
            return

        # 分支 2：用户在跟别人交流/暂时不需要bot插嘴 → 跳过回复，挂起会话30秒
        if conf_skip >= 0.5:
            self._log(gs.gid, text, f"用户({u_conv.uid})跳过本轮回复 conf_skip={conf_skip:.2f}")
            self._schedule_conversation_timeout(gs, u_conv, 30)
            self.store.save_debounced()
            return

        # 分支 3：继续对话！直接调用 _wake 放行当前 event 走 AstrBot 官方原生主 LLM 管线！
        # 100% 携带八千代人设、上下文历史、关系本、场景感知、输出过滤，并写入对话数据库！
        self._log(gs.gid, text, f"用户({u_conv.uid})继续对话→放行官方原生管线 conf_end={conf_end:.2f} conf_skip={conf_skip:.2f}")
        await self._wake(event, gs, uid)

    def _exit_conversation(self, gs, u_conv) -> None:
        if u_conv.timeout_task:
            u_conv.timeout_task.cancel()
            u_conv.timeout_task = None
        if u_conv.judge_task:
            u_conv.judge_task.cancel()
            u_conv.judge_task = None
        gs.conversations.pop(u_conv.uid, None)
        self.store.save_debounced()

    def _schedule_conversation_timeout(self, gs, u_conv, bot_text_len: int) -> None:
        """按 bot 回复字数走超时曲线安排该用户退出。"""
        timeout = interp(self._cfg("timeout_curve") or [[0, 10], [300, 60]], bot_text_len)
        if u_conv.timeout_task:
            u_conv.timeout_task.cancel()
        u_conv.timeout_task = self._spawn(
            self._conv_timeout_task(gs.gid, u_conv.uid, timeout)
        )

    async def _conv_timeout_task(self, gid: str, uid: str, timeout: float) -> None:
        try:
            await asyncio.sleep(max(1.0, timeout))
            gs = self.store.get(gid)
            u_conv = gs.conversations.get(uid)
            if u_conv:
                self._log(gid, "", f"用户({uid})会话超时({timeout:.0f}s)自动退出")
                self._exit_conversation(gs, u_conv)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[smart_wake] 会话超时任务异常: {e}")

    # ---------- bot 发言观察 ----------

    @filter.on_decorating_result(priority=20)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if self._closing or event.get_extra("_smart_wake_festival"):
            return
        try:
            gid = event.get_group_id()
            result = event.get_result()
            if not gid or not result:
                return
            bot_text = (result.get_plain_text() or "").strip()
            if not bot_text:
                return
            gs = self.store.get(str(gid))
            gs.bot_replies.append(bot_text)
            gs.buffer.append({"nickname": f"{self._cfg('bot_name') or 'bot'}(bot自己)",
                              "text": bot_text, "is_bot": True, "ts": time.time()})
            uid = str(event.get_sender_id())
            u_conv = gs.conversations.get(uid)
            # 原生唤醒也补登记该用户的 conversation 态
            if not u_conv and not gs.muted:
                u_conv = gs.get_or_create_conv(uid, gs.umo)
                self._consume_energy(gs)
            if u_conv:
                u_conv.waiting_llm = False
                u_conv.last_bot_reply_len = len(bot_text)
            self.store.save_debounced()
        except Exception as e:
            logger.warning(f"[smart_wake] on_decorating_result 异常: {e}")

    @filter.after_message_sent(priority=20)
    async def after_message_sent(self, event: AstrMessageEvent):
        """官方发送后钩子：为普通用户会话开始超时倒计时。"""
        if self._closing or event.get_extra("_smart_wake_festival"):
            return
        try:
            gid = event.get_group_id()
            if not gid:
                return
            gs = self.store.get(str(gid))
            uid = str(event.get_sender_id())
            u_conv = gs.conversations.get(uid)
            if u_conv and u_conv.last_bot_reply_len > 0:
                self._schedule_conversation_timeout(gs, u_conv, u_conv.last_bot_reply_len)
                u_conv.last_bot_reply_len = 0
                self.store.save_debounced()
        except Exception as e:
            logger.warning(f"[smart_wake] after_message_sent 异常: {e}")

    # ---------- 闭嘴状态机 ----------

    async def _judge_shutup(self, event, is_wake: bool, gs, text, nickname, now) -> None:
        bot_name = self._get_bot_name_str()
        state = {
            "bot_name": bot_name,
            "recent_messages": [f"{m['nickname']}: {m['text']}" for m in list(gs.buffer)[-10:]],
        }
        try:
            questions = build_shutup_questions(
                bot_name,
                list(gs.bot_replies),
                shutup_template=self._cfg("prompt_shutup") or "",
            )
            resp = await self.jev.decide(state, questions)
        except Exception as e:
            self._log(gs.gid, text, f"闭嘴判定Jev失败: {e}")
            return
        conf = float(((resp.get("answers") or {}).get("shut_up") or {}).get("noul", 0) or 0)
        threshold = int(self._cfg("mute_threshold", 50)) / 100
        self._log(gs.gid, text, f"闭嘴判定 conf={conf:.2f} 阈值={threshold:.2f} → {'进入muted' if conf >= threshold else '忽略'}")
        if conf >= threshold:
            gs.muted = True
            gs.mute_cool_ts = []
            for u_conv in list(gs.conversations.values()):
                self._exit_conversation(gs, u_conv)
            self.store.save_debounced()
            if is_wake:
                event.stop_event()

    async def _handle_muted(self, event, gs, text, is_wake, nickname, now) -> None:
        """muted 期间：只做冷却检测和解除检测。"""
        gs.mute_cool_ts.append(now)
        need = int(self._cfg("cool_check_msgs", 10))
        if len(gs.mute_cool_ts) >= need:
            window = float(self._cfg("cool_minutes", 3)) * 60
            if gs.mute_cool_ts[-need] < now - window:
                # 最近 need 条凑不满窗口 → 冷了，自动解除
                gs.muted = False
                gs.mute_cool_ts = []
                self._log(gs.gid, "", "群冷了，自动解除 muted")
                self.store.save_debounced()
                return
            gs.mute_cool_ts = gs.mute_cool_ts[-need:]

        # 解除检测：命中解除词或被 @bot
        unmute_candidate = bool(
            any(w and w in text for w in (self._cfg("unmute_words") or DEFAULT_UNMUTE_WORDS))
            or is_wake
        )
        if not unmute_candidate:
            return
        bot_name = self._get_bot_name_str()
        state = {
            "bot_name": bot_name,
            "recent_messages": [f"{m['nickname']}: {m['text']}" for m in list(gs.buffer)[-10:]],
        }
        try:
            questions = build_unmute_questions(
                bot_name,
                unmute_template=self._cfg("prompt_unmute") or "",
            )
            resp = await self.jev.decide(state, questions)
        except Exception as e:
            self._log(gs.gid, text, f"解除判定Jev失败: {e}")
            if is_wake:
                event.stop_event()  # 判定失败仍压住原生唤醒
            return
        conf = float(((resp.get("answers") or {}).get("unmute") or {}).get("noul", 0) or 0)
        threshold = int(self._cfg("unmute_threshold", 60)) / 100
        if conf >= threshold:
            gs.muted = False
            gs.mute_cool_ts = []
            self._log(gs.gid, text, f"解除 muted conf={conf:.2f}")
            self.store.save_debounced()
            return
        # 不通过 → 压住原生 @ 唤醒
        self._log(gs.gid, text, f"解除判定未过 conf={conf:.2f}，保持 muted")
        if is_wake:
            event.stop_event()

    # ---------- Web API ----------

    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[smart_wake] 当前 AstrBot 版本不支持 register_web_api，跳过注册")
            return
        reg = self.context.register_web_api
        prefix = "/astrbot_plugin_smart_wake"
        reg(f"{prefix}/config", self.api_get_config, ["GET"], "获取智能唤醒配置")
        reg(f"{prefix}/config/save", self.api_save_config, ["POST"], "保存智能唤醒配置")
        reg(f"{prefix}/test_jev", self.api_test_jev, ["POST"], "测试 Jev API 连接")
        reg(f"{prefix}/logs", self.api_logs, ["GET"], "获取决策日志")
        reg(f"{prefix}/status", self.api_status, ["GET"], "获取每群状态总览")

    async def api_get_config(self):
        try:
            data = {k: self.config.get(k) for k in SCALAR_KEYS}
            data["topics"] = self.config.get("topics") or []
            data["timeout_curve"] = self.config.get("timeout_curve") or []
            data["energy_cost_curve"] = self.config.get("energy_cost_curve") or []
            return jsonify({"success": True, "config": data})
        except Exception as exc:
            logger.error(f"[smart_wake] api_get_config 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_save_config(self):
        try:
            data = await request.get_json() or {}
            cfg = data.get("config")
            if not isinstance(cfg, dict):
                return jsonify({"success": False, "error": "config 缺失"}), 400
            for k in SCALAR_KEYS:
                if k in cfg:
                    self.config[k] = cfg[k]
            if isinstance(cfg.get("topics"), list):
                self.config["topics"] = [self._sanitize_topic(t) for t in cfg["topics"] if isinstance(t, dict)]
            if isinstance(cfg.get("timeout_curve"), list):
                self.config["timeout_curve"] = normalize_points(cfg["timeout_curve"])
            if isinstance(cfg.get("energy_cost_curve"), list):
                self.config["energy_cost_curve"] = normalize_points(cfg["energy_cost_curve"])
            self.config.save_config()
            self._sync_energy_max()
            self._save_curves_backup()
            return jsonify({"success": True, "message": "保存成功"})
        except Exception as exc:
            logger.error(f"[smart_wake] api_save_config 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @staticmethod
    def _sanitize_topic(t: dict) -> dict:
        return {
            "key": str(t.get("key") or "").strip(),
            "name": str(t.get("name") or "").strip(),
            "words": [str(w).strip() for w in (t.get("words") or []) if str(w).strip()],
            "score": max(0, min(10, int(t.get("score") or 0))),
            "cooldown_seconds": max(0, int(t.get("cooldown_seconds") or 0)),
            "is_question": bool(t.get("is_question", False)),
        }

    async def api_test_jev(self):
        try:
            ok, detail = await self.jev.test_connection()
            return jsonify({"success": ok, "detail": detail})
        except Exception as exc:
            logger.error(f"[smart_wake] api_test_jev 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_logs(self):
        try:
            return jsonify({"success": True, "logs": list(self.logs)})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_status(self):
        try:
            now = time.time()
            emax = self._sync_energy_max()
            groups = []
            for gid, gs in self.store.groups.items():
                cooldowns = {}
                for key, until in gs.topic_cooldowns.items():
                    remain = until - now
                    if remain > 0:
                        cooldowns[key] = round(remain)
                groups.append({
                    "group": gid,
                    "energy": round(gs.energy, 1),
                    "energy_max": emax,
                    "muted": gs.muted,
                    "conversation": bool(gs.conversations),
                    "active_users": [c.uid for c in gs.conversations.values()],
                    "heat_5min": gs.heat_count(now),
                    "topic_cooldowns": cooldowns,
                })
            return jsonify({"success": True, "groups": groups})
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

    async def terminate(self):
        self._closing = True
        tasks = set(self._tasks)
        for gs in self.store.groups.values():
            for u_conv in gs.conversations.values():
                tasks.update(t for t in (u_conv.timeout_task, u_conv.judge_task) if t is not None)
        tasks.discard(asyncio.current_task())
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
            self.store.close()
