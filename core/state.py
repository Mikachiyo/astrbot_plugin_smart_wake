"""每群状态：消息缓冲 / 热度 / 精力 / 冷却 / muted / conversation / 复读窗口 + 持久化。

持久化到 get_astrbot_data_path() 下的插件目录，JSON 单文件，
变更后防抖写盘（5 秒内合并）。
"""

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .reread import RereadDetector

BUFFER_MAX = 50          # 每群环形消息缓冲
HEAT_WINDOW = 300        # 热度滑动窗口（5 分钟）
BOT_REPLIES_KEEP = 3     # 记录的 bot 最近发言条数（闭嘴判定用）
SAVE_DEBOUNCE = 5.0      # 写盘防抖秒数


class UserConvState:
    """单个用户的会话状态。"""

    def __init__(self, uid: str, umo: str = ""):
        self.uid: str = str(uid)
        self.umo: str = str(umo)
        self.start_ts: float = time.time()
        self.conv_buffer: list[dict] = []                    # 该用户的结束判定缓冲
        self.waiting_llm: bool = False                       # bot 正在等该用户的 LLM 推理
        self.timeout_task: asyncio.Task | None = None        # 该用户的超时任务
        self.judge_task: asyncio.Task | None = None          # 结束判定防抖任务
        self.last_bot_reply_len: int = 0                     # 针对该用户发出的 bot 回复字数

    def to_dict(self) -> dict:
        return {"uid": self.uid, "start_ts": self.start_ts, "umo": self.umo}


class GroupState:
    """单个群的全部运行时状态。仅标量字段持久化。"""

    def __init__(self, gid: str):
        self.gid = gid
        self.buffer: deque[dict] = deque(maxlen=BUFFER_MAX)  # {nickname, text, is_bot, ts}
        self.heat: deque[float] = deque()                    # 消息时间戳（滑窗）
        self.energy: float = 100.0
        self.topic_cooldowns: dict[str, float] = {}          # 话题 key -> 冷却截止 ts
        self.muted: bool = False
        self.mute_cool_ts: list[float] = []                  # muted 期间冷却检测消息时间戳
        self.conversations: dict[str, UserConvState] = {}    # uid -> UserConvState（按用户独立会话）
        self.reread = RereadDetector()
        self.festival_watch: dict | None = None              # {fingerprint, deadline}
        self.umo: str = ""                                   # 该群 unified_msg_origin
        self.bot_replies: deque[str] = deque(maxlen=BOT_REPLIES_KEEP)
        self.msg_counter: int = 0                            # 精力按消息恢复计数

    def get_or_create_conv(self, uid: str, umo: str = "") -> UserConvState:
        uid_str = str(uid)
        if uid_str not in self.conversations:
            self.conversations[uid_str] = UserConvState(uid_str, umo or self.umo)
        elif umo:
            self.conversations[uid_str].umo = umo
        return self.conversations[uid_str]

    # ----- 热度 -----
    def add_heat(self, now: float) -> int:
        self.heat.append(now)
        while self.heat and self.heat[0] < now - HEAT_WINDOW:
            self.heat.popleft()
        return len(self.heat)

    def heat_count(self, now: float) -> int:
        while self.heat and self.heat[0] < now - HEAT_WINDOW:
            self.heat.popleft()
        return len(self.heat)

    # ----- 序列化（只存需要重启不丢的字段） -----
    def to_dict(self) -> dict:
        return {
            "energy": self.energy,
            "topic_cooldowns": dict(self.topic_cooldowns),
            "muted": self.muted,
            "conversations": {uid: c.to_dict() for uid, c in self.conversations.items()},
            "umo": self.umo,
        }

    def load_dict(self, d: dict) -> None:
        self.energy = float(d.get("energy", 100.0))
        self.topic_cooldowns = {str(k): float(v) for k, v in (d.get("topic_cooldowns") or {}).items()}
        self.muted = bool(d.get("muted", False))
        self.umo = str(d.get("umo") or "")
        self.conversations = {}
        # 兼容新的多用户会话表
        convs = d.get("conversations")
        if isinstance(convs, dict):
            for uid, cdata in convs.items():
                if isinstance(cdata, dict):
                    c = UserConvState(str(uid), str(cdata.get("umo") or self.umo))
                    c.start_ts = float(cdata.get("start_ts", time.time()))
                    self.conversations[str(uid)] = c
        # 兼容旧单会话
        old_conv = d.get("conversation")
        if isinstance(old_conv, dict) and "uid" in old_conv:
            ouid = str(old_conv["uid"])
            if ouid not in self.conversations:
                c = UserConvState(ouid, str(old_conv.get("umo") or self.umo))
                c.start_ts = float(old_conv.get("start_ts", time.time()))
                self.conversations[ouid] = c


class StateStore:
    """全部群状态 + 防抖持久化。"""

    def __init__(self, energy_default: float = 100.0):
        self.groups: dict[str, GroupState] = {}
        self._energy_default = energy_default
        self._dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_smart_wake"
        self._file = self._dir / "state.json"
        self._save_handle: asyncio.TimerHandle | None = None
        self.load()

    def get(self, gid: str) -> GroupState:
        gid = str(gid)
        if gid not in self.groups:
            gs = GroupState(gid)
            gs.energy = self._energy_default
            self.groups[gid] = gs
        return self.groups[gid]

    def set_energy_max(self, energy_max: float) -> bool:
        self._energy_default = max(0.0, energy_max)
        changed = False
        for gs in self.groups.values():
            energy = max(0.0, min(self._energy_default, gs.energy))
            changed |= energy != gs.energy
            gs.energy = energy
        return changed

    def close(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        self._save_now()

    # ----- 持久化 -----
    def load(self) -> None:
        try:
            if self._file.exists():
                data = json.loads(self._file.read_text(encoding="utf-8"))
                for gid, d in (data.get("groups") or {}).items():
                    gs = GroupState(str(gid))
                    gs.load_dict(d or {})
                    self.groups[str(gid)] = gs
                logger.info(f"[smart_wake] 已恢复 {len(self.groups)} 个群的状态")
        except Exception as e:
            logger.warning(f"[smart_wake] 状态恢复失败: {e}")

    def save_debounced(self) -> None:
        """变更后调用，5 秒内合并写盘。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_now()
            return
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._save_handle = loop.call_later(SAVE_DEBOUNCE, self._save_now)

    def _save_now(self) -> None:
        self._save_handle = None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "groups": {gid: gs.to_dict() for gid, gs in self.groups.items()},
            }
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._file)
        except Exception as e:
            logger.warning(f"[smart_wake] 状态保存失败: {e}")
