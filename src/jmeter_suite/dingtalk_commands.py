"""解析钉钉群内 @机器人命令并维护唯一目标群绑定。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from threading import Lock
from zoneinfo import ZoneInfo

from .dingtalk_binding import (
    DingTalkBinding,
    DingTalkBindingError,
    DingTalkBindingStore,
)


_BIND_COMMAND = "绑定巡检群"
_RUN_COMMAND = "执行巡检"
_UNBIND_COMMAND = "解绑巡检群"
_HELP_TEXT = "支持的命令：绑定巡检群、执行巡检、解绑巡检群。"
_MAX_SEEN_MESSAGES = 2048
_LEADING_MENTION = re.compile(r"^(?:@\S+\s*)+")


@dataclass(frozen=True, slots=True)
class DingTalkIncomingMessage:
    message_id: str
    conversation_id: str
    conversation_title: str
    sender_staff_id: str
    sender_nick: str
    is_admin: bool
    is_in_at_list: bool
    conversation_type: str
    message_type: str
    text: str


class DingTalkCommandController:
    def __init__(
        self,
        *,
        binding_store: DingTalkBindingStore,
        submit_run: Callable[[], bool],
        now: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ):
        self._binding_store = binding_store
        self._submit_run = submit_run
        self._now = now or (
            lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
        )
        self._logger = logger or logging.getLogger(__name__)
        self._lock = Lock()
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()

    def handle(
        self,
        message: DingTalkIncomingMessage,
        reply: Callable[[str], None],
    ) -> None:
        if not self._is_command_message(message):
            return
        with self._lock:
            if self._already_seen(message.message_id):
                return
            command = _normalize_command(message.text)
            try:
                if command == _BIND_COMMAND:
                    self._bind(message, reply)
                elif command == _RUN_COMMAND:
                    self._run(message, reply)
                elif command == _UNBIND_COMMAND:
                    self._unbind(message, reply)
                else:
                    reply(_HELP_TEXT)
            except DingTalkBindingError as error:
                self._logger.error("钉钉群绑定状态处理失败: %s", error)
                reply("巡检群绑定状态异常，请检查调度日志。")

    @staticmethod
    def _is_command_message(message: DingTalkIncomingMessage) -> bool:
        return (
            message.conversation_type == "2"
            and message.message_type == "text"
            and message.is_in_at_list
        )

    def _already_seen(self, message_id: str) -> bool:
        if message_id and message_id in self._seen_message_ids:
            return True
        if message_id:
            self._seen_message_ids[message_id] = None
            while len(self._seen_message_ids) > _MAX_SEEN_MESSAGES:
                self._seen_message_ids.popitem(last=False)
        return False

    def _bind(
        self,
        message: DingTalkIncomingMessage,
        reply: Callable[[str], None],
    ) -> None:
        current = self._binding_store.load()
        if current is not None:
            if current.open_conversation_id == message.conversation_id:
                reply("本群已经是已绑定的巡检群。")
            else:
                reply("机器人已绑定其他巡检群，请先在原群解绑。")
            return
        if not message.conversation_id or not message.sender_staff_id:
            reply("无法读取群或操作人员标识，本次绑定失败。")
            return
        self._binding_store.save(
            DingTalkBinding(
                open_conversation_id=message.conversation_id,
                conversation_title=message.conversation_title or "",
                bound_by_staff_id=message.sender_staff_id,
                bound_at=self._now(),
            )
        )
        reply("巡检群绑定成功，后续定时报告将发送到本群。")

    def _run(
        self,
        message: DingTalkIncomingMessage,
        reply: Callable[[str], None],
    ) -> None:
        current = self._binding_store.load()
        if current is None:
            reply("尚未绑定巡检群，请先发送“绑定巡检群”。")
            return
        if current.open_conversation_id != message.conversation_id:
            reply("本群不是已绑定的巡检群，不能执行巡检。")
            return
        if self._submit_run():
            reply("已接收，开始执行自动化巡检。")
        else:
            reply("已有巡检正在执行，本次未启动。")

    def _unbind(
        self,
        message: DingTalkIncomingMessage,
        reply: Callable[[str], None],
    ) -> None:
        current = self._binding_store.load()
        if current is None:
            reply("当前没有已绑定的巡检群。")
            return
        if current.open_conversation_id != message.conversation_id:
            reply("只能在当前已绑定的巡检群中解绑。")
            return
        self._binding_store.clear()
        reply("巡检群已解绑，定时报告将暂不发送到钉钉。")


def _normalize_command(text: str) -> str:
    normalized = (text or "").strip()
    return _LEADING_MENTION.sub("", normalized).strip()
