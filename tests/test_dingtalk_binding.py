"""验证钉钉目标群绑定状态的持久化和损坏数据处理。"""

from __future__ import annotations

from datetime import datetime
import importlib
import json
from pathlib import Path

import pytest


def _binding_module():
    return importlib.import_module("jmeter_suite.dingtalk_binding")


def test_binding_store_round_trips_one_group_and_replaces_atomically(
    tmp_path: Path,
):
    """A restart must recover the exact group without leaving temp files."""
    module = _binding_module()
    path = tmp_path / ".runtime" / "dingtalk-binding.json"
    store = module.DingTalkBindingStore(path)
    binding = module.DingTalkBinding(
        open_conversation_id="cid-bound-group",
        conversation_title="巡检通知群",
        bound_by_staff_id="staff-001",
        bound_at=datetime.fromisoformat("2026-08-12T09:30:00+08:00"),
    )

    assert store.load() is None
    store.save(binding)

    assert store.load() == binding
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "open_conversation_id": "cid-bound-group",
        "conversation_title": "巡检通知群",
        "bound_by_staff_id": "staff-001",
        "bound_at": "2026-08-12T09:30:00+08:00",
    }
    assert list(path.parent.glob("*.tmp")) == []


def test_binding_store_clear_is_idempotent(tmp_path: Path):
    """Unbinding twice must leave the service safely unbound."""
    module = _binding_module()
    path = tmp_path / "dingtalk-binding.json"
    store = module.DingTalkBindingStore(path)
    binding = module.DingTalkBinding(
        open_conversation_id="cid-bound-group",
        conversation_title="巡检通知群",
        bound_by_staff_id="staff-001",
        bound_at=datetime.fromisoformat("2026-08-12T09:30:00+08:00"),
    )
    store.save(binding)

    store.clear()
    store.clear()

    assert store.load() is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"schema_version": 2}',
        '{"schema_version": 1, "open_conversation_id": ""}',
        (
            '{"schema_version": 1, '
            '"open_conversation_id": "cid", '
            '"conversation_title": "group", '
            '"bound_by_staff_id": "staff", '
            '"bound_at": "not-a-time"}'
        ),
    ],
)
def test_binding_store_rejects_corrupt_state(
    tmp_path: Path,
    payload: str,
):
    """Corrupt state must never silently target an unintended group."""
    module = _binding_module()
    path = tmp_path / "dingtalk-binding.json"
    path.write_text(payload, encoding="utf-8")
    store = module.DingTalkBindingStore(path)

    with pytest.raises(module.DingTalkBindingError, match="绑定状态文件损坏"):
        store.load()
