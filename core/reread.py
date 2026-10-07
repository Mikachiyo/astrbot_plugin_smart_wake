"""复读检测（整合自 Zhalslar/astrbot_plugin_reread，MIT License）。

原项目: https://github.com/Zhalslar/astrbot_plugin_reread
保留版权声明；本插件只整合了核心跟读判定逻辑，被打断/禁言功能未整合。

每群按消息类型各维护滑动窗口（文本/图片/表情），窗口尾部连续 N 条同指纹
且来自不同发送者 → 触发跟读；同一发送者连续刷屏清空窗口。
"""

from collections import deque


def make_fingerprint(chain) -> tuple[str, str] | None:
    """从消息链生成 (类型, 指纹)。只处理单段消息，无法识别返回 None。"""
    try:
        from astrbot.core.message.components import Plain, Image, Face
    except Exception:  # pragma: no cover
        return None
    if not chain or len(chain) != 1:
        return None
    seg = chain[0]
    if isinstance(seg, Plain):
        text = (seg.text or "").strip()
        return ("text", f"text:{text}") if text else None
    if isinstance(seg, Image):
        ident = getattr(seg, "file", None) or getattr(seg, "url", None) or getattr(seg, "path", None)
        return ("image", f"image:{ident}") if ident else None
    if isinstance(seg, Face):
        return ("face", f"face:{getattr(seg, 'id', '')}")
    return None


class RereadDetector:
    """单群的复读窗口集合。"""

    WINDOW_MAX = 10

    def __init__(self):
        # kind -> deque[(fingerprint, sender_id)]
        self.windows: dict[str, deque] = {
            "text": deque(maxlen=self.WINDOW_MAX),
            "image": deque(maxlen=self.WINDOW_MAX),
            "face": deque(maxlen=self.WINDOW_MAX),
        }
        self.last_handled_fingerprint: str | None = None
        self.last_handled_at: float = 0.0

    def process(self, kind: str, fingerprint: str, sender: str, threshold: int,
                suppression_seconds: float = 0.0, now: float | None = None) -> bool:
        """按类型分别计数；0 保留旧的最近指纹抑制，正数在到期后重新累计。"""
        import time
        now = time.monotonic() if now is None else now
        if (suppression_seconds > 0 and self.last_handled_fingerprint is not None
                and now - self.last_handled_at >= suppression_seconds):
            previous = self.last_handled_fingerprint
            self.last_handled_fingerprint = None
            for window in self.windows.values():
                if any(fp == previous for fp, _ in window):
                    window.clear()
        if not fingerprint or fingerprint == self.last_handled_fingerprint:
            return False
        win = self.windows.setdefault(kind, deque(maxlen=self.WINDOW_MAX))
        # 同一发送者连续刷屏 → 清空窗口重计
        if win and win[-1][1] == sender:
            win.clear()
        win.append((fingerprint, sender))
        threshold = max(2, int(threshold or 2))
        if len(win) < threshold:
            return False
        tail = list(win)[-threshold:]
        fps = {f for f, _ in tail}
        senders = {s for _, s in tail}
        if len(fps) == 1 and len(senders) == threshold:
            self.last_handled_fingerprint = fingerprint
            self.last_handled_at = now
            return True
        return False
