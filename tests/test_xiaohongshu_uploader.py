import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import uploader.xiaohongshu_uploader.main as xhs_main


class FakeLocator:
    def __init__(self, name, count=0, src=None, children=None):
        self.name = name
        self._count = count
        self._src = src
        self._children = children or {}

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self._children.get(selector, FakeLocator(selector))

    def get_by_text(self, text, exact=False):
        return self._children.get(f"text:{text}", FakeLocator(text))

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    async def count(self):
        return self._count

    async def wait_for(self, **kwargs):
        return None

    async def get_attribute(self, name):
        if name == "src":
            return self._src
        return None

    async def fill(self, value):
        return None

    async def click(self):
        return None


class RecordingKeyboard:
    def __init__(self):
        self.actions = []

    async def press(self, key):
        self.actions.append(("press", key))

    async def type(self, text, delay=None):
        self.actions.append(("type", text, delay))


class RecordingLocator(FakeLocator):
    def __init__(self, name):
        super().__init__(name, count=1)
        self.actions = []

    async def fill(self, value):
        self.actions.append(("fill", value))

    async def click(self):
        self.actions.append(("click",))

    async def wait_for(self, **kwargs):
        self.actions.append(("wait_for", kwargs))


class RecordingPage:
    def __init__(self):
        self.keyboard = RecordingKeyboard()
        self.locators = {
            'input[placeholder*="填写标题"]': RecordingLocator("title"),
            'p[data-placeholder*="输入正文描述"]': RecordingLocator("desc"),
            '#creator-editor-topic-container': RecordingLocator("topic-container"),
            '#creator-editor-topic-container .item': RecordingLocator("topic-item"),
        }

    def locator(self, selector):
        return self.locators[selector]


class PublishButton:
    def __init__(self, page, *, target_url=None, click_error=None, disabled=False):
        self.page = page
        self.target_url = target_url
        self.click_error = click_error
        self.disabled = disabled
        self.click_count = 0

    @property
    def first(self):
        return self

    async def wait_for(self, **kwargs):
        return None

    async def is_disabled(self):
        return self.disabled

    async def click(self, **kwargs):
        self.click_count += 1
        if self.target_url:
            self.page.url = self.target_url
        if self.click_error:
            raise self.click_error


class PublishPage:
    def __init__(self, url, *, target_url=None, click_error=None, disabled=False):
        self.url = url
        self.button = PublishButton(
            self,
            target_url=target_url,
            click_error=click_error,
            disabled=disabled,
        )
        self.requested_button = None

    def get_by_role(self, role, *, name, exact):
        self.requested_button = (role, name, exact)
        return self.button


class XiaohongshuUploaderTests(unittest.TestCase):
    def setUp(self):
        self.external_action_gate = patch.dict(
            os.environ,
            {"SAU_ENABLE_EXTERNAL_ACTIONS": "true"},
        )
        self.external_action_gate.start()
        self.addCleanup(self.external_action_gate.stop)

    def test_public_feed_url_parser_returns_stable_object(self):
        result = xhs_main.parse_xiaohongshu_public_feed_url(
            "https://www.xiaohongshu.com/explore/64F1A2B3C4D5E6F7A8B9C0D1"
            "?xsec_token=secret-not-copied"
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "PUBLISHED")
        self.assertEqual(result.feed_id, "64f1a2b3c4d5e6f7a8b9c0d1")
        self.assertEqual(
            result.object_ref,
            "xiaohongshu:feed:64f1a2b3c4d5e6f7a8b9c0d1",
        )
        self.assertEqual(
            result.evidence_url,
            "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1",
        )
        self.assertNotIn("xsec_token", result.evidence_url)

    def test_public_feed_url_parser_rejects_wrong_domain_query_guess_and_missing_id(self):
        invalid_urls = (
            "https://www.xiaohongshu.com.example/explore/64f1a2b3c4d5e6f7a8b9c0d1",
            "http://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1",
            "https://creator.xiaohongshu.com/publish/success?noteId=64f1a2b3c4d5e6f7a8b9c0d1",
            "https://www.xiaohongshu.com/search_result?keyword=64f1a2b3c4d5e6f7a8b9c0d1",
            "https://www.xiaohongshu.com/explore/",
            "https://www.xiaohongshu.com/explore/not-a-feed-id",
            "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1/edit",
        )

        for raw_url in invalid_urls:
            with self.subTest(raw_url=raw_url):
                self.assertIsNone(
                    xhs_main.parse_xiaohongshu_public_feed_url(raw_url)
                )

    def test_immediate_publish_clicks_once_and_returns_verified_feed(self):
        app = xhs_main.XiaoHongShuVideo(
            title="demo",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        page = PublishPage(
            "https://creator.xiaohongshu.com/publish/publish",
            target_url="https://www.xiaohongshu.com/discovery/item/64f1a2b3c4d5e6f7a8b9c0d1",
        )

        result = asyncio.run(app.submit_publish_once(page))

        self.assertEqual(result.status, "PUBLISHED")
        self.assertEqual(page.button.click_count, 1)
        self.assertEqual(page.requested_button, ("button", "发布", True))
        with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError):
            asyncio.run(app.submit_publish_once(page))
        self.assertEqual(page.button.click_count, 1)

    def test_publish_without_stable_id_is_unknown_and_never_reclicks(self):
        app = xhs_main.XiaoHongShuNote(
            image_paths=["image.png"],
            note="demo",
            tags=[],
            publish_date=0,
            account_file="account.json",
            title="demo",
        )
        page = PublishPage("https://creator.xiaohongshu.com/publish/success?ok=1")
        unknown = xhs_main.XiaohongshuPublishUnknownError(
            "UNKNOWN: no stable feed id; do not retry"
        )

        with patch(
            "uploader.xiaohongshu_uploader.main._wait_for_verified_publish_result",
            new=AsyncMock(side_effect=unknown),
        ):
            with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError) as caught:
                asyncio.run(app.submit_publish_once(page))

        self.assertEqual(caught.exception.status, "UNKNOWN")
        self.assertFalse(caught.exception.retry_safe)
        self.assertEqual(page.button.click_count, 1)
        with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError):
            asyncio.run(app.submit_publish_once(page))
        self.assertEqual(page.button.click_count, 1)

    def test_untrusted_result_url_times_out_as_unknown_without_guessing_query_id(self):
        page = PublishPage(
            "https://creator.xiaohongshu.com/publish/success"
            "?noteId=64f1a2b3c4d5e6f7a8b9c0d1"
        )

        with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError) as caught:
            asyncio.run(
                xhs_main._wait_for_verified_publish_result(
                    page,
                    publish_strategy=xhs_main.XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
                    timeout_seconds=0,
                    poll_seconds=0.01,
                )
            )

        self.assertIn("禁止重发", str(caught.exception))

    def test_click_exception_is_unknown_because_external_action_may_have_happened(self):
        app = xhs_main.XiaoHongShuVideo(
            title="demo",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        page = PublishPage(
            "https://creator.xiaohongshu.com/publish/publish",
            click_error=TimeoutError("navigation raced click"),
        )

        with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError):
            asyncio.run(app.submit_publish_once(page))

        self.assertTrue(app._publish_attempted)
        self.assertEqual(page.button.click_count, 1)

    def test_scheduled_submission_is_unknown_not_published(self):
        app = xhs_main.XiaoHongShuNote(
            image_paths=["image.png"],
            note="demo",
            tags=[],
            publish_date=0,
            account_file="account.json",
            title="demo",
            publish_strategy=xhs_main.XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        )
        page = PublishPage("https://creator.xiaohongshu.com/publish/success?scheduled=true")

        with self.assertRaises(xhs_main.XiaohongshuPublishUnknownError) as caught:
            asyncio.run(app.submit_publish_once(page))

        self.assertIn("不能冒充已发布", str(caught.exception))
        self.assertEqual(page.requested_button, ("button", "定时发布", True))
        self.assertEqual(page.button.click_count, 1)

    def test_material_wait_is_bounded_and_does_not_click(self):
        calls = 0

        async def never_ready():
            nonlocal calls
            calls += 1
            return False

        with self.assertRaises(TimeoutError) as caught:
            asyncio.run(
                xhs_main._bounded_wait(
                    never_ready,
                    timeout_seconds=0,
                    poll_seconds=0.01,
                    timeout_message="等待素材超时，未点击发布",
                )
            )

        self.assertEqual(calls, 1)
        self.assertIn("未点击发布", str(caught.exception))

    def test_direct_uploader_keeps_external_action_gate(self):
        app = xhs_main.XiaoHongShuVideo(
            title="demo",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "External publishing is disabled"):
                asyncio.run(app.upload(None))

    def test_submit_boundary_gate_blocks_before_click(self):
        app = xhs_main.XiaoHongShuVideo(
            title="demo",
            file_path="demo.mp4",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )
        page = PublishPage("https://creator.xiaohongshu.com/publish/publish")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "External publishing is disabled"):
                asyncio.run(app.submit_publish_once(page))

        self.assertFalse(app._publish_attempted)
        self.assertEqual(page.button.click_count, 0)

    def test_creator_urls_keep_xiaohongshu_domain_by_default(self):
        with patch.dict(os.environ, {"SAU_XHS_CREATOR_BASE_URL": ""}):
            self.assertEqual(
                xhs_main._build_xhs_creator_url("/login"),
                "https://creator.xiaohongshu.com/login",
            )

    def test_creator_urls_use_configured_rednote_domain(self):
        with patch.dict(
            os.environ,
            {"SAU_XHS_CREATOR_BASE_URL": "https://creator.rednote.com/"},
        ):
            self.assertEqual(
                xhs_main._build_xhs_creator_url("/login"),
                "https://creator.rednote.com/login",
            )
            self.assertEqual(
                xhs_main._build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                ),
                "https://creator.rednote.com/publish/publish?from=homepage&target=video",
            )

    def test_find_xhs_qrcode_locator_prefers_scan_sibling_inside_login_box(self):
        qrcode_locator = FakeLocator("qrcode", count=1, src="data:image/png;base64,abc")
        scan_text_locator = FakeLocator(
            "scan-text",
            count=1,
            children={
                "xpath=..//following-sibling::div//img": qrcode_locator,
            },
        )
        login_box_locator = FakeLocator(
            "login-box",
            count=1,
            children={
                "div:has-text('扫一扫')": scan_text_locator,
                "text:APP扫一扫登录": scan_text_locator,
            },
        )
        page = FakeLocator(
            "page",
            children={
                "div[class*='login-box']": login_box_locator,
                ".login-box-container": login_box_locator,
            },
        )

        locator = asyncio.run(xhs_main._find_xhs_qrcode_locator(page))
        self.assertIs(locator, qrcode_locator)

    def test_setup_returns_detail_when_cookie_invalid_without_handle(self):
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            result = asyncio.run(
                xhs_main.xiaohongshu_setup(
                    "missing.json",
                    handle=False,
                    return_detail=True,
                )
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "cookie_invalid")

    def test_setup_uses_login_flow_when_handle_is_true(self):
        login_result = {
            "success": True,
            "status": "success",
            "message": "ok",
            "account_file": "account.json",
            "qrcode": {"image_path": "qrcode.png"},
            "current_url": "https://creator.xiaohongshu.com/",
        }
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            with patch(
                "uploader.xiaohongshu_uploader.main.xiaohongshu_cookie_gen",
                new=AsyncMock(return_value=login_result),
            ) as mock_login:
                result = asyncio.run(
                    xhs_main.xiaohongshu_setup(
                        "account.json",
                        handle=True,
                        return_detail=True,
                    )
                )
        self.assertTrue(result["success"])
        mock_login.assert_awaited_once()

    def test_video_validate_upload_args_normalizes_video_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            thumbnail_path = Path(tmp_dir) / "demo.png"
            cookie_path = Path(tmp_dir) / "account.json"
            video_path.write_bytes(b"video")
            thumbnail_path.write_bytes(b"image")
            cookie_path.write_text("{}")

            app = xhs_main.XiaoHongShuVideo(
                title="demo",
                file_path=str(video_path),
                tags=["xhs"],
                publish_date=0,
                account_file=str(cookie_path),
                thumbnail_path=str(thumbnail_path),
            )

            with patch(
                "uploader.xiaohongshu_uploader.main.cookie_auth",
                new=AsyncMock(return_value=True),
            ):
                asyncio.run(app.validate_upload_args())

        self.assertTrue(app.file_path.endswith("demo.mp4"))
        self.assertTrue(app.thumbnail_path.endswith("demo.png"))

    def test_note_uploader_exists_and_validates_required_fields(self):
        note_cls = getattr(xhs_main, "XiaoHongShuNote")
        app = note_cls(
            image_paths=[],
            note="",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )

        with patch.object(app, "validate_base_args", new=AsyncMock(return_value=None)):
            with self.assertRaises(ValueError):
                asyncio.run(app.validate_upload_args())

    def test_video_fill_meta_uses_desc_then_first_tag(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
            desc="描述内容",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['input[placeholder*="填写标题"]'].actions,
            [("fill", "标题内容")],
        )
        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertIn(("type", "描述内容", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)
        self.assertEqual(
            page.locators['#creator-editor-topic-container .item'].actions,
            [("wait_for", {"state": "visible", "timeout": 2000}), ("click",)],
        )

    def test_video_fill_meta_can_fill_first_tag_without_desc(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertNotIn(("type", "", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)

    def test_note_title_defaults_do_not_override_explicit_title(self):
        app = xhs_main.XiaoHongShuNote(
            image_paths=["a.png"],
            note="正文",
            tags=[],
            publish_date=0,
            account_file="account.json",
            title="显式标题",
            desc="图文正文",
        )

        self.assertEqual(app.title, "显式标题")
        self.assertEqual(app.desc, "图文正文")


if __name__ == "__main__":
    unittest.main()
