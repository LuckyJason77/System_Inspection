"""验证钉钉群命令的绑定、群范围、去重和执行反馈。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jmeter_suite.dingtalk_binding import DingTalkBindingStore
from jmeter_suite.dingtalk_commands import (
    DingTalkCommandController,
    DingTalkIncomingMessage,
)


FIXED_NOW = datetime.fromisoformat("2026-08-12T10:15:30+08:00")


def _message(
    text: str,
    *,
    message_id: str = "msg-1",
    conversation_id: str = "cid-main",
    staff_id: str = "staff-1",
    is_admin: bool = False,
    is_at: bool = True,
    conversation_type: str = "2",
    message_type: str = "text",
) -> DingTalkIncomingMessage:
    return DingTalkIncomingMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        conversation_title="巡检群",
        sender_staff_id=staff_id,
        sender_nick="测试人员",
        is_admin=is_admin,
        is_in_at_list=is_at,
        conversation_type=conversation_type,
        message_type=message_type,
        text=text,
    )


def _controller(
    tmp_path: Path,
    submit_run=lambda: True,
) -> tuple[DingTalkCommandController, DingTalkBindingStore]:
    store = DingTalkBindingStore(tmp_path / "dingtalk-binding.json")
    controller = DingTalkCommandController(
        binding_store=store,
        submit_run=submit_run,
        now=lambda: FIXED_NOW,
    )
    return controller, store


def test_any_group_member_can_bind_the_first_group(tmp_path: Path):
    """The platform admin flag must not block a member from binding."""
    controller, store = _controller(tmp_path)
    replies: list[str] = []

    controller.handle(_message("绑定巡检群"), replies.append)

    assert replies == ["巡检群绑定成功，后续定时报告将发送到本群。"]
    binding = store.load()
    assert binding is not None
    assert binding.open_conversation_id == "cid-main"
    assert binding.bound_by_staff_id == "staff-1"
    assert binding.bound_at == FIXED_NOW


def test_every_member_of_the_bound_group_can_execute_once(tmp_path: Path):
    """Manual execution must not be restricted to group administrators."""
    submissions = 0

    def submit() -> bool:
        nonlocal submissions
        submissions += 1
        return True

    controller, _ = _controller(tmp_path, submit)
    controller.handle(_message("绑定巡检群", is_admin=True), lambda _: None)
    replies: list[str] = []

    controller.handle(
        _message(
            "执行巡检",
            message_id="msg-2",
            staff_id="ordinary-member",
            is_admin=False,
        ),
        replies.append,
    )

    assert submissions == 1
    assert replies == ["已接收，开始执行自动化巡检。"]


def test_execute_replies_busy_without_queueing(tmp_path: Path):
    """A busy service must reject instead of queuing a later inspection."""
    controller, _ = _controller(tmp_path, lambda: False)
    controller.handle(_message("绑定巡检群", is_admin=True), lambda _: None)
    replies: list[str] = []

    controller.handle(
        _message("执行巡检", message_id="msg-2"),
        replies.append,
    )

    assert replies == ["已有巡检正在执行，本次未启动。"]


def test_execute_is_rejected_outside_the_bound_group(tmp_path: Path):
    """Installing the same bot elsewhere must not expose the runner."""
    submissions = 0

    def submit() -> bool:
        nonlocal submissions
        submissions += 1
        return True

    controller, _ = _controller(tmp_path, submit)
    controller.handle(_message("绑定巡检群", is_admin=True), lambda _: None)
    replies: list[str] = []

    controller.handle(
        _message(
            "执行巡检",
            message_id="msg-2",
            conversation_id="cid-other",
        ),
        replies.append,
    )

    assert submissions == 0
    assert replies == ["本群不是已绑定的巡检群，不能执行巡检。"]


def test_any_member_of_current_group_can_unbind(tmp_path: Path):
    """Membership in the bound conversation, not isAdmin, controls scope."""
    controller, store = _controller(tmp_path)
    controller.handle(_message("绑定巡检群"), lambda _: None)
    replies: list[str] = []

    controller.handle(
        _message(
            "解绑巡检群",
            message_id="msg-2",
            conversation_id="cid-other",
            staff_id="staff-2",
        ),
        replies.append,
    )
    controller.handle(
        _message(
            "解绑巡检群",
            message_id="msg-3",
            staff_id="staff-2",
        ),
        replies.append,
    )

    assert replies == [
        "只能在当前已绑定的巡检群中解绑。",
        "巡检群已解绑，定时报告将暂不发送到钉钉。",
    ]
    assert store.load() is None


def test_execute_without_binding_tells_any_member_how_to_bind(
    tmp_path: Path,
):
    """The help reply must not repeat the removed admin restriction."""
    controller, _ = _controller(tmp_path)
    replies: list[str] = []

    controller.handle(_message("执行巡检"), replies.append)

    assert replies == ["尚未绑定巡检群，请先发送“绑定巡检群”。"]


def test_duplicate_message_id_starts_only_one_run(tmp_path: Path):
    """A Stream callback retry must not launch JMeter twice."""
    submissions = 0

    def submit() -> bool:
        nonlocal submissions
        submissions += 1
        return True

    controller, _ = _controller(tmp_path, submit)
    controller.handle(_message("绑定巡检群", is_admin=True), lambda _: None)
    message = _message("执行巡检", message_id="duplicate")

    controller.handle(message, lambda _: None)
    controller.handle(message, lambda _: None)

    assert submissions == 1


def test_non_group_non_at_and_non_text_messages_are_ignored(tmp_path: Path):
    """Only an explicit group mention may reach command handling."""
    controller, store = _controller(tmp_path)
    replies: list[str] = []

    controller.handle(
        _message("绑定巡检群", conversation_type="1", is_admin=True),
        replies.append,
    )
    controller.handle(
        _message(
            "绑定巡检群",
            message_id="msg-2",
            is_at=False,
            is_admin=True,
        ),
        replies.append,
    )
    controller.handle(
        _message(
            "绑定巡检群",
            message_id="msg-3",
            message_type="picture",
            is_admin=True,
        ),
        replies.append,
    )

    assert replies == []
    assert store.load() is None


def test_unknown_at_command_returns_the_supported_commands(tmp_path: Path):
    """A discoverable help reply prevents accidental command guessing."""
    controller, _ = _controller(tmp_path)
    replies: list[str] = []

    controller.handle(_message("现在运行吗"), replies.append)

    assert replies == [
        "支持的命令：绑定巡检群、执行巡检、解绑巡检群。"
    ]
