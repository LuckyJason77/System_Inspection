"""持久化调度服务绑定的唯一钉钉目标群。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any


_SCHEMA_VERSION = 1


class DingTalkBindingError(ValueError):
    """绑定状态文件无法安全读取或写入。"""


@dataclass(frozen=True, slots=True)
class DingTalkBinding:
    open_conversation_id: str
    conversation_title: str
    bound_by_staff_id: str
    bound_at: datetime


class DingTalkBindingStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> DingTalkBinding | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._parse(raw)
        except DingTalkBindingError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DingTalkBindingError(
                f"钉钉绑定状态文件损坏: {self.path} ({error})"
            ) from error

    def save(self, binding: DingTalkBinding) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "open_conversation_id": binding.open_conversation_id,
            "conversation_title": binding.conversation_title,
            "bound_by_staff_id": binding.bound_by_staff_id,
            "bound_at": binding.bound_at.isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    payload,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise DingTalkBindingError(
                f"无法保存钉钉绑定状态: {self.path} ({error})"
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise DingTalkBindingError(
                f"无法清除钉钉绑定状态: {self.path} ({error})"
            ) from error

    def _parse(self, raw: Any) -> DingTalkBinding:
        try:
            if not isinstance(raw, dict):
                raise ValueError("根节点必须是对象")
            if raw.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("schema_version 不受支持")
            conversation_id = _required_string(
                raw,
                "open_conversation_id",
            )
            conversation_title = raw.get("conversation_title", "")
            if not isinstance(conversation_title, str):
                raise ValueError("conversation_title 必须是字符串")
            staff_id = _required_string(raw, "bound_by_staff_id")
            bound_at = datetime.fromisoformat(
                _required_string(raw, "bound_at")
            )
            if bound_at.tzinfo is None:
                raise ValueError("bound_at 必须包含时区")
        except (TypeError, ValueError) as error:
            raise DingTalkBindingError(
                f"钉钉绑定状态文件损坏: {self.path} ({error})"
            ) from error
        return DingTalkBinding(
            open_conversation_id=conversation_id,
            conversation_title=conversation_title,
            bound_by_staff_id=staff_id,
            bound_at=bound_at,
        )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()
