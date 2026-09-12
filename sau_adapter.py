from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any


REQUEST_SCHEMA = "foundry.huaxiaobao.tool-request.v1"
RESPONSE_SCHEMA = "foundry.huaxiaobao.tool-result.v1"
DESCRIPTOR_SCHEMA = "foundry.huaxiaobao.capability-descriptor.v1"
SUPPORTED_PLATFORMS = (
    "alipay",
    "baijiahao",
    "bilibili",
    "douyin",
    "hupu",
    "kuaishou",
    "tencent",
    "weibo",
    "xiaohongshu",
    "youtube",
)

CHECKER_NAMES = {
    platform: f"check_{platform}_account" for platform in SUPPORTED_PLATFORMS
}
SENSITIVE_KEY_PARTS = ("cookie", "token", "password", "secret", "authorization", "session")


def _canonical_hash(request: dict[str, Any]) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _stable_account_object_ref(platform: str, account_ref: str) -> str:
    digest = hashlib.sha256(account_ref.encode()).hexdigest()[:16]
    # 故意不包含 executor：主执行器与 fallback 必须锁定同一个平台账号对象。
    return f"{platform}:account:{digest}"


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def descriptor() -> dict[str, Any]:
    return {
        "schema_version": DESCRIPTOR_SCHEMA,
        "executor_id": "social-auto-upload",
        "capabilities": [
            {
                "name": "account.status",
                "platform": platform,
                "side_effect": "read_only",
                "retry_safe": True,
                "executor_role": "fallback" if platform == "xiaohongshu" else "primary",
            }
            for platform in SUPPORTED_PLATFORMS
        ],
        "external_actions_available": False,
        "primary_executor_id": {"xiaohongshu": "xiaohongshu-mcp"},
    }


def _base_response(request: dict[str, Any]) -> dict[str, Any]:
    platform = str(request.get("platform", ""))
    return {
        "schema_version": RESPONSE_SCHEMA,
        "operation_id": str(request.get("operation_id", "")),
        "request_hash": _canonical_hash(request),
        "capability": str(request.get("capability", "")),
        "executor_id": f"social-auto-upload/{platform}" if platform else "social-auto-upload",
        "executor_role": "fallback" if platform == "xiaohongshu" else "primary",
        "side_effect": "read_only",
        "status": "UNKNOWN",
        "account_ref": str(request.get("account_ref", "")),
        "retry_safe": True,
        "external_action_performed": False,
    }


async def _load_checker(platform: str) -> Callable[[str], Awaitable[bool]]:
    module = importlib.import_module("sau_cli")
    return getattr(module, CHECKER_NAMES[platform])


async def execute_request(
    request: dict[str, Any],
    checker: Callable[[str], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    result = _base_response(request)
    required = ("schema_version", "operation_id", "capability", "platform", "account_ref")
    if any(not str(request.get(field, "")).strip() for field in required):
        result.update(
            status="REJECTED",
            error={"code": "INVALID_REQUEST", "message": "required fields are missing"},
        )
        return result
    if request["schema_version"] != REQUEST_SCHEMA:
        result.update(
            status="REJECTED",
            error={"code": "SCHEMA_VERSION_UNSUPPORTED", "message": "unsupported request schema"},
        )
        return result
    if _contains_sensitive_key(request):
        result.update(
            status="REJECTED",
            error={
                "code": "CREDENTIAL_MATERIAL_FORBIDDEN",
                "message": "credentials are not accepted in adapter requests",
            },
        )
        return result
    if request["capability"] != "account.status":
        result.update(
            status="REJECTED",
            error={
                "code": "CAPABILITY_NOT_AVAILABLE",
                "message": "the safe adapter does not expose publishing operations",
            },
        )
        return result
    platform = str(request["platform"])
    if platform not in SUPPORTED_PLATFORMS:
        result.update(
            status="REJECTED",
            error={"code": "PLATFORM_UNSUPPORTED", "message": "unsupported platform"},
        )
        return result
    account_ref = str(request["account_ref"])
    if (
        len(account_ref) > 128
        or ".." in account_ref
        or "/" in account_ref
        or "\\" in account_ref
        or "\x00" in account_ref
    ):
        result.update(
            status="REJECTED",
            error={"code": "INVALID_ACCOUNT_REF", "message": "unsafe account reference"},
        )
        return result

    try:
        account_checker = checker or await _load_checker(platform)
        ready = await account_checker(account_ref)
    except Exception as exc:
        result["error"] = {
            "code": "NATIVE_CHECK_FAILED",
            "message": "native account check failed",
            "error_type": type(exc).__name__,
        }
        return result

    result["object_ref"] = _stable_account_object_ref(platform, str(request["account_ref"]))
    result["details"] = {"is_ready": bool(ready)}
    if ready:
        result["status"] = "READY"
    else:
        result.update(
            status="BLOCKED",
            error={"code": "ACCOUNT_LOGIN_REQUIRED", "message": "account owner login is required"},
        )
    return result


def _read_request() -> dict[str, Any]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe Huaxiaobao adapter for social-auto-upload")
    parser.add_argument("command", choices=("describe", "execute"))
    args = parser.parse_args(argv)
    if args.command == "describe":
        print(json.dumps(descriptor(), ensure_ascii=False, sort_keys=True))
        return 0
    try:
        result = asyncio.run(execute_request(_read_request()))
    except Exception as exc:
        result = {
            "schema_version": RESPONSE_SCHEMA,
            "executor_id": "social-auto-upload",
            "status": "REJECTED",
            "external_action_performed": False,
            "error": {"code": "INVALID_JSON", "message": str(exc)},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result["status"] == "REJECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
