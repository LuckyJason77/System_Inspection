"""验证钉钉认证、媒体上传、群消息载荷和重试边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import jmeter_suite.dingtalk_api as dingtalk_api_module
from jmeter_suite.dingtalk_api import (
    DingTalkApiClient,
    DingTalkCredentialError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _client(
    session: FakeSession,
    *,
    sleeps: list[float] | None = None,
) -> DingTalkApiClient:
    observed_sleeps = sleeps if sleeps is not None else []
    return DingTalkApiClient(
        client_id="ding-client",
        client_secret="super-secret",
        session=session,
        monotonic=lambda: 100.0,
        sleep=observed_sleeps.append,
    )


def test_client_bypasses_proxy_for_dingtalk_without_losing_existing_rules(
    monkeypatch: pytest.MonkeyPatch,
):
    """DingTalk must bypass a local HTTPS proxy without replacing other rules."""
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    _client(FakeSession([]))
    _client(FakeSession([]))

    assert dingtalk_api_module.os.environ["NO_PROXY"].split(",") == [
        "localhost",
        "127.0.0.1",
        ".dingtalk.com",
    ]
    assert ".dingtalk.com" in (
        dingtalk_api_module.os.environ["no_proxy"].split(",")
    )


def test_markdown_send_uses_cached_token_and_exact_group_payload():
    """Wrong robot/group fields would make proactive scheduled sends fail."""
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"accessToken": "token-1", "expireIn": 7200},
            ),
            FakeResponse(200, {"processQueryKey": "message-1"}),
            FakeResponse(200, {"processQueryKey": "message-2"}),
        ]
    )
    client = _client(session)

    client.send_markdown("cid-main", "自动化巡检", "**通过**")
    client.send_markdown("cid-main", "自动化巡检", "**再次通过**")

    assert len(session.calls) == 3
    token_url, token_call = session.calls[0]
    assert token_url == "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    assert token_call["json"] == {
        "appKey": "ding-client",
        "appSecret": "super-secret",
    }
    send_url, first_send = session.calls[1]
    assert send_url == (
        "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
    )
    assert first_send["headers"] == {
        "x-acs-dingtalk-access-token": "token-1",
        "Content-Type": "application/json",
    }
    assert first_send["json"] == {
        "robotCode": "ding-client",
        "openConversationId": "cid-main",
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps(
            {"title": "自动化巡检", "text": "**通过**"},
            ensure_ascii=False,
        ),
    }


def test_upload_and_file_send_use_zip_contract(
    tmp_path: Path,
):
    """The attachment must use media upload followed by sampleFile."""
    archive_path = tmp_path / "自动化巡检报告_run.zip"
    archive_path.write_bytes(b"PK\x03\x04report")
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"accessToken": "token-1", "expireIn": 7200},
            ),
            FakeResponse(
                200,
                {"errcode": 0, "errmsg": "ok", "media_id": "@media-1"},
            ),
            FakeResponse(200, {"processQueryKey": "message-1"}),
        ]
    )
    client = _client(session)

    media_id = client.upload_file(archive_path)
    client.send_file(
        "cid-main",
        media_id,
        archive_path.name,
        "zip",
    )

    assert media_id == "@media-1"
    upload_url, upload_call = session.calls[1]
    assert upload_url == "https://oapi.dingtalk.com/media/upload"
    assert upload_call["params"] == {
        "access_token": "token-1",
        "type": "file",
    }
    upload_name, upload_stream, upload_mime = upload_call["files"]["media"]
    assert upload_name == archive_path.name
    assert upload_mime == "application/zip"
    assert upload_stream.closed is True
    send_call = session.calls[2][1]
    assert send_call["json"] == {
        "robotCode": "ding-client",
        "openConversationId": "cid-main",
        "msgKey": "sampleFile",
        "msgParam": json.dumps(
            {
                "mediaId": "@media-1",
                "fileName": archive_path.name,
                "fileType": "zip",
            },
            ensure_ascii=False,
        ),
    }


def test_transient_group_failure_retries_three_attempts():
    """A brief DingTalk outage must not drop a completed report immediately."""
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"accessToken": "token-1", "expireIn": 7200},
            ),
            FakeResponse(500, {"code": "system.error"}),
            requests.ConnectionError("temporary disconnect"),
            FakeResponse(200, {"processQueryKey": "message-1"}),
        ]
    )
    client = _client(session, sleeps=sleeps)

    client.send_markdown("cid-main", "自动化巡检", "结果")

    assert len(session.calls) == 4
    assert sleeps == [1.0, 2.0]


def test_unauthorized_send_refreshes_token_once():
    """An expired cached token must be replaced without losing the message."""
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"accessToken": "expired-token", "expireIn": 7200},
            ),
            FakeResponse(401, {"code": "InvalidAuthentication"}),
            FakeResponse(
                200,
                {"accessToken": "fresh-token", "expireIn": 7200},
            ),
            FakeResponse(200, {"processQueryKey": "message-1"}),
        ]
    )
    client = _client(session)

    client.send_markdown("cid-main", "自动化巡检", "结果")

    assert session.calls[1][1]["headers"][
        "x-acs-dingtalk-access-token"
    ] == "expired-token"
    assert session.calls[3][1]["headers"][
        "x-acs-dingtalk-access-token"
    ] == "fresh-token"


def test_invalid_credentials_never_expose_the_secret():
    """Credential diagnostics must be useful without leaking appSecret."""
    session = FakeSession(
        [
            FakeResponse(
                400,
                {"code": "invalid.app.key", "message": "bad credentials"},
            )
        ]
    )
    client = _client(session)

    with pytest.raises(DingTalkCredentialError) as captured:
        client.validate_credentials()

    assert "invalid.app.key" in str(captured.value)
    assert "super-secret" not in str(captured.value)
