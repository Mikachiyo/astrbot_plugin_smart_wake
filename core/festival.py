"""节日主动事件：交给官方事件队列执行完整 pipeline。"""

import asyncio

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import At, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import MessageMember
from astrbot.core.platform.message_session import MessageSession


class FestivalMessageEvent(CronMessageEvent):
    def __init__(self, context, *, umo: str, gid: str, self_id: str, prompt: str):
        session = MessageSession.from_str(umo)
        super().__init__(
            context=context,
            session=session,
            message="[smart_wake 节日定时主动触发，非用户发言] " + prompt,
            sender_id=self_id,
            sender_name="smart_wake 节日定时任务",
            message_type=session.message_type,
            extras={"_active_trigger": True, "active_reply_triggered": True},
        )
        self.message_obj.group_id = gid
        self.message_obj.sender = MessageMember(
            user_id="smart_wake:festival", nickname="smart_wake 节日定时任务"
        )
        # Cron 平台类型不重建 unique_session；session 保留触发时的完整 UMO。
        self.done = asyncio.get_running_loop().create_future()
        self.pipeline_task = None
        self.delivery_failed = False
        self.model_delivered = False
        self.delivered_texts = []
        self.send_allowed = lambda: True
        self.set_extra("_smart_wake_festival", True)
        self.set_extra("_api_key_allow_admin_role", False)
        self.set_extra("enable_streaming", False)

    async def send(self, message):
        if self.done.cancelled() or self.is_stopped() or not self.send_allowed():
            raise asyncio.CancelledError
        if message is None:
            return
        # 定时事件没有真实平台消息可引用，也不能 @ 合成的发送者。
        chain = [
            component for component in message.chain
            if not (
                isinstance(component, At)
                and str(component.qq) == self.get_sender_id()
            ) and not (
                isinstance(component, Reply)
                and str(component.id) == str(self.message_obj.message_id)
            )
        ]
        if not chain:
            return
        outgoing = message.derive(chain)
        try:
            if not await self.context_obj.send_message(self.session, outgoing):
                raise RuntimeError("节日主动消息未找到可用平台，发送失败")
        except Exception:
            self.delivery_failed = True
            raise
        await AstrMessageEvent.send(self, outgoing)
        result = self.get_result()
        if result and result.is_model_result():
            self.model_delivered = True
            text = outgoing.get_plain_text().strip()
            if text:
                self.delivered_texts.append(text)

    def cleanup_temporary_local_files(self):
        try:
            super().cleanup_temporary_local_files()
        finally:
            tool_texts = self.get_extra("_send_message_to_user_current_session_plain_texts", [])
            if isinstance(tool_texts, list):
                for text in tool_texts:
                    if isinstance(text, str) and text.strip():
                        self.model_delivered = True
                        if text.strip() not in self.delivered_texts:
                            self.delivered_texts.append(text.strip())
            if not self.done.done():
                self.done.set_result(self.model_delivered and not self.delivery_failed)
