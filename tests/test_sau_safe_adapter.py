import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from sau_adapter import REQUEST_SCHEMA, descriptor, execute_request
from sau_safety import external_action_guard, external_actions_enabled


def request(**overrides):
    value = {
        "schema_version": REQUEST_SCHEMA,
        "operation_id": "op-account-1",
        "capability": "account.status",
        "platform": "xiaohongshu",
        "account_ref": "opaque-account-ref",
    }
    value.update(overrides)
    return value


class SafeAdapterTests(unittest.TestCase):
    def test_descriptor_marks_xiaohongshu_as_fallback(self):
        capability = next(
            item for item in descriptor()["capabilities"] if item["platform"] == "xiaohongshu"
        )
        self.assertEqual(capability["executor_role"], "fallback")
        self.assertEqual(descriptor()["primary_executor_id"]["xiaohongshu"], "xiaohongshu-mcp")
        self.assertFalse(descriptor()["external_actions_available"])

    def test_account_status_is_structured_and_retry_safe(self):
        checker = AsyncMock(return_value=True)
        result = asyncio.run(execute_request(request(), checker=checker))

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["side_effect"], "read_only")
        self.assertTrue(result["retry_safe"])
        self.assertFalse(result["external_action_performed"])
        self.assertTrue(result["object_ref"].startswith("xiaohongshu:account:"))
        checker.assert_awaited_once_with("opaque-account-ref")

    def test_outbound_capability_is_rejected_without_calling_checker(self):
        checker = AsyncMock(return_value=True)
        result = asyncio.run(
            execute_request(request(capability="content.publish"), checker=checker)
        )

        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["error"]["code"], "CAPABILITY_NOT_AVAILABLE")
        checker.assert_not_awaited()

    def test_nested_credentials_and_unsafe_account_paths_are_rejected(self):
        checker = AsyncMock(return_value=True)
        credential = asyncio.run(
            execute_request(request(payload={"session_token": "not-accepted"}), checker)
        )
        traversal = asyncio.run(execute_request(request(account_ref="../escape"), checker))

        self.assertEqual(credential["error"]["code"], "CREDENTIAL_MATERIAL_FORBIDDEN")
        self.assertEqual(traversal["error"]["code"], "INVALID_ACCOUNT_REF")
        checker.assert_not_awaited()

    def test_legacy_upload_guard_is_fail_closed(self):
        guarded_call = AsyncMock(return_value="sent")
        guarded = external_action_guard(guarded_call)

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(external_actions_enabled())
            with self.assertRaisesRegex(RuntimeError, "disabled by default"):
                asyncio.run(guarded())
        guarded_call.assert_not_awaited()

    def test_legacy_upload_guard_requires_explicit_true(self):
        guarded_call = AsyncMock(return_value="sent")
        guarded = external_action_guard(guarded_call)

        with patch.dict(os.environ, {"SAU_ENABLE_EXTERNAL_ACTIONS": "true"}, clear=True):
            self.assertTrue(external_actions_enabled())
            self.assertEqual(asyncio.run(guarded()), "sent")
        guarded_call.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
