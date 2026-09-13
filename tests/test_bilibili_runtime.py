import hashlib
import io
import json
import stat
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from uploader.bilibili_uploader.runtime import (
    BiliupDownloadError,
    BiliupRuntimeIntegrityError,
    build_biliup_runtime_path,
    download_biliup_asset,
    ensure_biliup_binary,
    load_biliup_lock,
    run_biliup_command,
)

VERSION = "v9.8.7"


def zip_asset(payload: bytes = b"windows-binary") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("release/biliup.exe", payload)
    return buffer.getvalue()


def tar_asset(payload: bytes = b"linux-binary") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        info = tarfile.TarInfo("release/biliup")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_lock(
    path: Path,
    *,
    platform_key: str,
    asset_name: str,
    archive: bytes,
    sha256: str | None = None,
) -> None:
    value = {
        "schema_version": "social-auto-upload.biliup-lock.v1",
        "version": VERSION,
        "source_release": f"https://github.com/biliup/biliup/releases/tag/{VERSION}",
        "platforms": {
            platform_key: {
                "asset_name": asset_name,
                "asset_url": (
                    f"https://github.com/biliup/biliup/releases/download/"
                    f"{VERSION}/{asset_name}"
                ),
                "size": len(archive),
                "sha256": sha256 or hashlib.sha256(archive).hexdigest(),
            }
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None, stream_error=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.stream_error = stream_error
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("response URL and body must not escape")

    def iter_content(self, chunk_size):
        del chunk_size
        midpoint = max(1, len(self.body) // 2)
        yield self.body[:midpoint]
        if self.stream_error is not None:
            raise self.stream_error
        yield self.body[midpoint:]

    def close(self):
        self.closed = True


class FakeDownloader:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected download")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class BiliupRuntimeTests(unittest.TestCase):
    def test_repository_lock_is_valid_and_pins_supported_assets(self):
        lock = load_biliup_lock()
        self.assertEqual(lock["version"], "v1.2.4")
        self.assertIn("windows-x86_64", lock["platforms"])
        self.assertIn("linux-x86_64", lock["platforms"])
        self.assertIn("linux-aarch64", lock["platforms"])
        for asset in lock["platforms"].values():
            self.assertNotIn("latest", asset["asset_url"])
            self.assertEqual(len(asset["sha256"]), 64)

    def test_build_biliup_runtime_path_returns_windows_and_linux_paths(self):
        windows = build_biliup_runtime_path("Windows", "AMD64")
        linux = build_biliup_runtime_path("Linux", "x86_64")
        self.assertTrue(str(windows).endswith("windows-x86_64\\biliup.exe"))
        self.assertTrue(str(linux).replace("\\", "/").endswith("linux-x86_64/biliup"))

    def test_windows_install_is_verified_atomic_and_idempotent(self):
        archive = zip_asset()
        response = FakeResponse(
            archive,
            headers={"Content-Length": str(len(archive))},
        )
        downloader = FakeDownloader(response)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "lock.json"
            write_lock(
                lock_path,
                platform_key="windows-x86_64",
                asset_name=f"biliupR-{VERSION}-x86_64-windows.zip",
                archive=archive,
            )
            with patch(
                "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                return_value=root / "runtime",
            ):
                first = ensure_biliup_binary(
                    system_name="Windows",
                    machine_name="AMD64",
                    lock_path=lock_path,
                    downloader=downloader,
                )
                second = ensure_biliup_binary(
                    system_name="Windows",
                    machine_name="AMD64",
                    lock_path=lock_path,
                    downloader=downloader,
                )

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"windows-binary")
            self.assertEqual(len(downloader.calls), 1)
            self.assertTrue(response.closed)
            install = json.loads(first.with_name("install.json").read_text("utf-8"))
            self.assertEqual(install["version"], VERSION)
            self.assertEqual(
                install["asset_sha256"], hashlib.sha256(archive).hexdigest()
            )
            self.assertFalse(list(first.parent.glob("*.tmp")))

    def test_linux_tar_install_sets_executable_bit(self):
        archive = tar_asset()
        downloader = FakeDownloader(FakeResponse(archive))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "lock.json"
            write_lock(
                lock_path,
                platform_key="linux-x86_64",
                asset_name=f"biliupR-{VERSION}-x86_64-linux.tar.xz",
                archive=archive,
            )
            with (
                patch(
                    "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                    return_value=root / "runtime",
                ),
                patch("pathlib.Path.chmod", autospec=True) as chmod,
            ):
                binary = ensure_biliup_binary(
                    system_name="Linux",
                    machine_name="x86_64",
                    lock_path=lock_path,
                    downloader=downloader,
                )
            self.assertEqual(binary.read_bytes(), b"linux-binary")
            chmod.assert_called_once()
            self.assertTrue(chmod.call_args.args[0].name.endswith(".tmp"))
            self.assertTrue(chmod.call_args.args[1] & stat.S_IXUSR)

    def test_allowed_github_asset_redirect_is_bounded(self):
        archive = zip_asset()
        redirect = FakeResponse(
            status=302,
            headers={
                "Location": (
                    "https://release-assets.githubusercontent.com/github-production-release-asset/"
                    "asset?download=1"
                )
            },
        )
        payload = FakeResponse(archive)
        downloader = FakeDownloader(redirect, payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "lock.json"
            write_lock(
                lock_path,
                platform_key="windows-x86_64",
                asset_name=f"biliupR-{VERSION}-x86_64-windows.zip",
                archive=archive,
            )
            with patch(
                "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                return_value=root / "runtime",
            ):
                ensure_biliup_binary(
                    system_name="Windows",
                    machine_name="AMD64",
                    lock_path=lock_path,
                    downloader=downloader,
                )
        self.assertEqual(len(downloader.calls), 2)
        self.assertFalse(downloader.calls[0][1]["allow_redirects"])

    def test_cross_domain_redirect_fails_without_leaking_url_data(self):
        archive = zip_asset()
        redirect = FakeResponse(
            status=302,
            headers={"Location": "https://evil.invalid/file?token=must-not-leak"},
        )
        asset = {
            "version": VERSION,
            "platform": "windows-x86_64",
            "asset_name": f"biliupR-{VERSION}-x86_64-windows.zip",
            "asset_url": (
                f"https://github.com/biliup/biliup/releases/download/{VERSION}/"
                f"biliupR-{VERSION}-x86_64-windows.zip"
            ),
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "biliup.exe"
            with self.assertRaises(BiliupDownloadError) as raised:
                download_biliup_asset(
                    asset,
                    destination,
                    downloader=FakeDownloader(redirect),
                )
            self.assertFalse(destination.exists())
        self.assertNotIn("must-not-leak", str(raised.exception))
        self.assertTrue(redirect.closed)

    def test_lock_rejects_latest_and_non_https_urls_before_network(self):
        archive = zip_asset()
        for unsafe_url in (
            "https://github.com/biliup/biliup/releases/latest/download/biliup.zip",
            f"http://github.com/biliup/biliup/releases/download/{VERSION}/biliup.zip",
        ):
            with (
                self.subTest(unsafe_url=unsafe_url),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                lock_path = root / "lock.json"
                asset_name = f"biliupR-{VERSION}-x86_64-windows.zip"
                write_lock(
                    lock_path,
                    platform_key="windows-x86_64",
                    asset_name=asset_name,
                    archive=archive,
                )
                lock = json.loads(lock_path.read_text("utf-8"))
                lock["platforms"]["windows-x86_64"]["asset_url"] = unsafe_url
                lock_path.write_text(json.dumps(lock), encoding="utf-8")
                downloader = FakeDownloader()
                with (
                    patch(
                        "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                        return_value=root / "runtime",
                    ),
                    self.assertRaises(BiliupRuntimeIntegrityError),
                ):
                    ensure_biliup_binary(
                        system_name="Windows",
                        machine_name="AMD64",
                        lock_path=lock_path,
                        downloader=downloader,
                    )
                self.assertFalse(downloader.calls)

    def test_timeout_and_partial_stream_leave_no_install_files(self):
        archive = zip_asset()
        cases = (
            requests.Timeout("secret-url"),
            FakeResponse(
                archive,
                stream_error=requests.Timeout("signed-url-must-not-leak"),
            ),
        )
        for case in cases:
            with (
                self.subTest(case=type(case).__name__),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                lock_path = root / "lock.json"
                write_lock(
                    lock_path,
                    platform_key="windows-x86_64",
                    asset_name=f"biliupR-{VERSION}-x86_64-windows.zip",
                    archive=archive,
                )
                with (
                    patch(
                        "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                        return_value=root / "runtime",
                    ),
                    self.assertRaises(BiliupDownloadError) as raised,
                ):
                    ensure_biliup_binary(
                        system_name="Windows",
                        machine_name="AMD64",
                        lock_path=lock_path,
                        downloader=FakeDownloader(case),
                    )
                self.assertNotIn("secret", str(raised.exception))
                runtime_root = root / "runtime"
                self.assertFalse(list(runtime_root.rglob("biliup.exe")))
                self.assertFalse(list(runtime_root.rglob("install.json")))
                self.assertFalse(list(runtime_root.rglob("*.tmp")))

    def test_response_larger_than_lock_is_stopped_before_install(self):
        archive = zip_asset()
        asset = {
            "version": VERSION,
            "platform": "windows-x86_64",
            "asset_name": f"biliupR-{VERSION}-x86_64-windows.zip",
            "asset_url": (
                f"https://github.com/biliup/biliup/releases/download/{VERSION}/"
                f"biliupR-{VERSION}-x86_64-windows.zip"
            ),
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "biliup.exe"
            with self.assertRaises(BiliupDownloadError):
                download_biliup_asset(
                    asset,
                    destination,
                    downloader=FakeDownloader(FakeResponse(archive + b"overflow")),
                )
            self.assertFalse(destination.exists())
            self.assertFalse(list(destination.parent.glob("*.tmp")))

    def test_bad_hash_and_extraction_error_preserve_existing_binary_and_clean_temp(
        self,
    ):
        archive = zip_asset()
        for failure in ("hash", "extract"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                destination = root / "runtime" / "windows-x86_64" / "biliup.exe"
                destination.parent.mkdir(parents=True)
                destination.write_bytes(b"existing-binary")
                asset = {
                    "version": VERSION,
                    "platform": "windows-x86_64",
                    "asset_name": f"biliupR-{VERSION}-x86_64-windows.zip",
                    "asset_url": (
                        f"https://github.com/biliup/biliup/releases/download/{VERSION}/"
                        f"biliupR-{VERSION}-x86_64-windows.zip"
                    ),
                    "size": len(archive),
                    "sha256": (
                        "0" * 64
                        if failure == "hash"
                        else hashlib.sha256(archive).hexdigest()
                    ),
                }

                def fail_after_partial_extract(_archive, _asset_name, staged):
                    staged.write_bytes(b"partial")
                    raise BiliupRuntimeIntegrityError("partial extraction")

                extraction_patch = (
                    patch(
                        "uploader.bilibili_uploader.runtime._extract_binary",
                        side_effect=fail_after_partial_extract,
                    )
                    if failure == "extract"
                    else nullcontext()
                )
                with extraction_patch, self.assertRaises(BiliupRuntimeIntegrityError):
                    download_biliup_asset(
                        asset,
                        destination,
                        downloader=FakeDownloader(FakeResponse(archive)),
                    )
                self.assertEqual(destination.read_bytes(), b"existing-binary")
                self.assertFalse(list(destination.parent.glob("*.tmp")))

    def test_same_version_with_different_locked_hash_fails_closed(self):
        archive = zip_asset()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "lock.json"
            write_lock(
                lock_path,
                platform_key="windows-x86_64",
                asset_name=f"biliupR-{VERSION}-x86_64-windows.zip",
                archive=archive,
            )
            first_downloader = FakeDownloader(FakeResponse(archive))
            with patch(
                "uploader.bilibili_uploader.runtime.get_biliup_runtime_root",
                return_value=root / "runtime",
            ):
                ensure_biliup_binary(
                    system_name="Windows",
                    machine_name="AMD64",
                    lock_path=lock_path,
                    downloader=first_downloader,
                )
                write_lock(
                    lock_path,
                    platform_key="windows-x86_64",
                    asset_name=f"biliupR-{VERSION}-x86_64-windows.zip",
                    archive=archive,
                    sha256="1" * 64,
                )
                second_downloader = FakeDownloader()
                with self.assertRaises(BiliupRuntimeIntegrityError):
                    ensure_biliup_binary(
                        system_name="Windows",
                        machine_name="AMD64",
                        lock_path=lock_path,
                        downloader=second_downloader,
                    )
            self.assertFalse(second_downloader.calls)

    @patch("uploader.bilibili_uploader.runtime.subprocess.run")
    @patch("uploader.bilibili_uploader.runtime.ensure_biliup_binary")
    def test_run_biliup_command_returns_completed_process(
        self, mock_ensure_binary, mock_run
    ):
        mock_ensure_binary.return_value = Path("C:/mock/biliup.exe")
        mock_run.return_value = Mock(returncode=0, stdout="ok", stderr="")
        result = run_biliup_command(["login"])
        self.assertEqual(result.returncode, 0)

    @patch("uploader.bilibili_uploader.runtime.subprocess.run")
    @patch("uploader.bilibili_uploader.runtime.ensure_biliup_binary")
    def test_run_biliup_command_login_uses_interactive_stdio(
        self, mock_ensure_binary, mock_run
    ):
        mock_ensure_binary.return_value = Path("C:/mock/biliup.exe")
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        run_biliup_command(["login"], interactive=True)
        _, kwargs = mock_run.call_args
        self.assertNotIn("capture_output", kwargs)
