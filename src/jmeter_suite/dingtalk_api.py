"""封装钉钉 accessToken、媒体上传和企业机器人群消息接口。"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any

import requests


_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_GROUP_SEND_URL = (
    "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
)
_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
_TOKEN_REFRESH_BUFFER_SECONDS = 5 * 60
_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (1.0, 2.0)
_LEGACY_EXPIRED_TOKEN_CODES = {40014, 42001}
_DINGTALK_NO_PROXY = ".dingtalk.com"
_NO_PROXY_LOCK = Lock()


def configure_dingtalk_no_proxy() -> None:
    """让全部钉钉子域名绕过环境中的 HTTP/HTTPS 代理。"""
    with _NO_PROXY_LOCK:
        for variable_name in ("NO_PROXY", "no_proxy"):
            entries = [
                entry.strip()
                for entry in os.environ.get(variable_name, "").split(",")
                if entry.strip()
            ]
            normalized = {
                entry.lower().lstrip("*.")
                for entry in entries
            }
            if "*" not in entries and "dingtalk.com" not in normalized:
                entries.append(_DINGTALK_NO_PROXY)
            os.environ[variable_name] = ",".join(entries)


class DingTalkApiError(RuntimeError):
    """钉钉接口返回不可恢复的错误。"""


class DingTalkCredentialError(DingTalkApiError):
    """Client ID 或 Client Secret 无法换取 accessToken。"""


class DingTalkTransientError(DingTalkApiError):
    """钉钉网络、限流或服务端暂时不可用。"""


class DingTalkApiClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        session: requests.Session | Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        request_timeout_seconds: float = 10.0,
    ):
        configure_dingtalk_no_proxy()
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._monotonic = monotonic
        self._sleep = sleep
        self._request_timeout_seconds = request_timeout_seconds
        self._token_lock = Lock()
        self._access_token = ""
        self._access_token_expires_at = 0.0

    def validate_credentials(self) -> None:
        self._get_access_token(force=True)

    def send_markdown(
        self,
        conversation_id: str,
        title: str,
        text: str,
    ) -> None:
        self._send_group_message(
            conversation_id,
            "sampleMarkdown",
            {"title": title, "text": text},
        )

    def send_file(
        self,
        conversation_id: str,
        media_id: str,
        file_name: str,
        file_type: str,
    ) -> None:
        self._send_group_message(
            conversation_id,
            "sampleFile",
            {
                "mediaId": media_id,
                "fileName": file_name,
                "fileType": file_type,
            },
        )

    def upload_file(self, path: Path) -> str:
        source = Path(path)
        if not source.is_file():
            raise DingTalkApiError(f"待上传文件不存在: {source}")
        for authentication_attempt in range(2):
            token = self._get_access_token()

            def upload() -> Any:
                with source.open("rb") as source_file:
                    return self._session.post(
                        _MEDIA_UPLOAD_URL,
                        params={
                            "access_token": token,
                            "type": "file",
                        },
                        files={
                            "media": (
                                source.name,
                                source_file,
                                "application/zip",
                            )
                        },
                        timeout=self._request_timeout_seconds,
                    )

            response = self._request_with_retry(upload, "媒体文件上传")
            if response.status_code == 401 and authentication_attempt == 0:
                self._invalidate_access_token()
                continue
            self._raise_for_http_error(response, "媒体文件上传")
            payload = self._response_json(response, "媒体文件上传")
            errcode = payload.get("errcode", 0)
            if (
                errcode in _LEGACY_EXPIRED_TOKEN_CODES
                and authentication_attempt == 0
            ):
                self._invalidate_access_token()
                continue
            if errcode != 0:
                raise DingTalkApiError(
                    _api_error_message("媒体文件上传", response, payload)
                )
            media_id = payload.get("media_id")
            if not isinstance(media_id, str) or not media_id:
                raise DingTalkApiError("媒体文件上传响应缺少 media_id")
            return media_id
        raise DingTalkApiError("媒体文件上传认证失败")

    def _send_group_message(
        self,
        conversation_id: str,
        message_key: str,
        message_parameters: dict[str, object],
    ) -> None:
        body = {
            "robotCode": self._client_id,
            "openConversationId": conversation_id,
            "msgKey": message_key,
            "msgParam": json.dumps(
                message_parameters,
                ensure_ascii=False,
            ),
        }
        for authentication_attempt in range(2):
            token = self._get_access_token()
            response = self._request_with_retry(
                lambda: self._session.post(
                    _GROUP_SEND_URL,
                    headers={
                        "x-acs-dingtalk-access-token": token,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=self._request_timeout_seconds,
                ),
                "机器人群消息发送",
            )
            if response.status_code == 401 and authentication_attempt == 0:
                self._invalidate_access_token()
                continue
            self._raise_for_http_error(response, "机器人群消息发送")
            return
        raise DingTalkApiError("机器人群消息发送认证失败")

    def _get_access_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            now = self._monotonic()
            if (
                not force
                and self._access_token
                and now < self._access_token_expires_at
            ):
                return self._access_token
            response = self._request_with_retry(
                lambda: self._session.post(
                    _TOKEN_URL,
                    json={
                        "appKey": self._client_id,
                        "appSecret": self._client_secret,
                    },
                    timeout=self._request_timeout_seconds,
                ),
                "钉钉凭据校验",
            )
            payload = self._response_json(response, "钉钉凭据校验")
            if response.status_code >= 400:
                raise DingTalkCredentialError(
                    _api_error_message("钉钉凭据校验", response, payload)
                )
            token = payload.get("accessToken")
            expire_in = payload.get("expireIn")
            if (
                not isinstance(token, str)
                or not token
                or isinstance(expire_in, bool)
                or not isinstance(expire_in, (int, float))
            ):
                raise DingTalkCredentialError(
                    "钉钉凭据校验响应缺少 accessToken 或 expireIn"
                )
            self._access_token = token
            self._access_token_expires_at = now + max(
                0.0,
                float(expire_in) - _TOKEN_REFRESH_BUFFER_SECONDS,
            )
            return token

    def _invalidate_access_token(self) -> None:
        with self._token_lock:
            self._access_token = ""
            self._access_token_expires_at = 0.0

    def _request_with_retry(
        self,
        operation: Callable[[], Any],
        operation_name: str,
    ) -> Any:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = operation()
            except requests.RequestException as error:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise DingTalkTransientError(
                        f"{operation_name}网络请求失败: "
                        f"{type(error).__name__}"
                    ) from error
                self._sleep(_RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise DingTalkTransientError(
                        f"{operation_name}暂时不可用: "
                        f"HTTP {response.status_code}"
                    )
                self._sleep(_RETRY_DELAYS_SECONDS[attempt])
                continue
            return response
        raise DingTalkTransientError(f"{operation_name}重试耗尽")

    @staticmethod
    def _response_json(response: Any, operation_name: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise DingTalkApiError(
                f"{operation_name}返回了无效 JSON"
            ) from error
        if not isinstance(payload, dict):
            raise DingTalkApiError(f"{operation_name}返回值必须是对象")
        return payload

    def _raise_for_http_error(
        self,
        response: Any,
        operation_name: str,
    ) -> None:
        if response.status_code < 400:
            return
        payload = self._response_json(response, operation_name)
        raise DingTalkApiError(
            _api_error_message(operation_name, response, payload)
        )


def _api_error_message(
    operation_name: str,
    response: Any,
    payload: dict[str, Any],
) -> str:
    code = payload.get("code", payload.get("errcode", "unknown"))
    message = payload.get("message", payload.get("errmsg", "未知错误"))
    return (
        f"{operation_name}失败: HTTP {response.status_code}, "
        f"code={code}, message={message}"
    )
