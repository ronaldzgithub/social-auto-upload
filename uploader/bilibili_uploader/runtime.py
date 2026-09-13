from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

LOCK_SCHEMA = "social-auto-upload.biliup-lock.v1"
INSTALL_RECORD_SCHEMA = "social-auto-upload.biliup-install.v1"
DEFAULT_LOCK_PATH = Path(__file__).with_name("biliup-lock.json")
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "release-assets.githubusercontent.com"}
)
MAX_REDIRECTS = 3
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BINARY_BYTES = 128 * 1024 * 1024
DOWNLOAD_TIMEOUT = (10, 30)
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class BiliupRuntimeIntegrityError(RuntimeError):
    """Pinned runtime metadata or bytes do not match their reviewed lock."""


class BiliupDownloadError(RuntimeError):
    """The bounded pinned-asset download could not complete safely."""


def get_biliup_runtime_root() -> Path:
    return Path.home() / ".social-auto-upload" / "tools" / "biliup"


def _normalize_system(system_name: str | None = None) -> str:
    system_value = (system_name or platform.system()).strip().lower()
    if system_value == "darwin":
        return "macos"
    return system_value


def _normalize_machine(machine_name: str | None = None) -> str:
    machine_value = (machine_name or platform.machine()).strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(machine_value, machine_value)


def _build_platform_key(
    system_name: str | None = None, machine_name: str | None = None
) -> str:
    return f"{_normalize_system(system_name)}-{_normalize_machine(machine_name)}"


def build_biliup_runtime_path(
    system_name: str | None = None, machine_name: str | None = None
) -> Path:
    executable_name = (
        "biliup.exe" if _normalize_system(system_name) == "windows" else "biliup"
    )
    return (
        get_biliup_runtime_root()
        / _build_platform_key(system_name, machine_name)
        / executable_name
    )


def _build_install_record_path(
    system_name: str | None = None, machine_name: str | None = None
) -> Path:
    return build_biliup_runtime_path(system_name, machine_name).with_name(
        "install.json"
    )


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_initial_asset_url(version: str, asset_name: str, asset_url: str) -> None:
    if Path(asset_name).name != asset_name or not asset_name:
        raise BiliupRuntimeIntegrityError("Locked biliup asset name is invalid")
    expected_url = (
        f"https://github.com/biliup/biliup/releases/download/{version}/{asset_name}"
    )
    if asset_url != expected_url:
        raise BiliupRuntimeIntegrityError(
            "Locked biliup asset URL is not the fixed release URL"
        )
    parsed = urlsplit(asset_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise BiliupRuntimeIntegrityError(
            "Locked biliup asset URL must use GitHub HTTPS"
        )
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise BiliupRuntimeIntegrityError(
            "Locked biliup asset URL contains forbidden authority data"
        )
    if parsed.query or parsed.fragment:
        raise BiliupRuntimeIntegrityError(
            "Locked biliup asset URL must not contain query or fragment data"
        )


def load_biliup_lock(lock_path: Path | None = None) -> dict[str, Any]:
    path = lock_path or DEFAULT_LOCK_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliupRuntimeIntegrityError(
            "Unable to read the reviewed biliup lock"
        ) from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "version",
        "source_release",
        "platforms",
    }:
        raise BiliupRuntimeIntegrityError("Biliup lock has unsupported fields")
    if value["schema_version"] != LOCK_SCHEMA:
        raise BiliupRuntimeIntegrityError("Biliup lock schema is unsupported")
    version = value["version"]
    if not isinstance(version, str) or not version.startswith("v") or not version[1:]:
        raise BiliupRuntimeIntegrityError("Biliup lock version is invalid")
    if (
        value["source_release"]
        != f"https://github.com/biliup/biliup/releases/tag/{version}"
    ):
        raise BiliupRuntimeIntegrityError(
            "Biliup source release does not match the locked version"
        )
    if not isinstance(value["platforms"], dict) or not value["platforms"]:
        raise BiliupRuntimeIntegrityError("Biliup lock has no platform assets")

    for platform_key, asset in value["platforms"].items():
        if not isinstance(platform_key, str) or not isinstance(asset, dict):
            raise BiliupRuntimeIntegrityError("Biliup platform lock entry is invalid")
        if set(asset) != {"asset_name", "asset_url", "size", "sha256"}:
            raise BiliupRuntimeIntegrityError("Biliup platform lock fields are invalid")
        if (
            isinstance(asset["size"], bool)
            or not isinstance(asset["size"], int)
            or not 1 <= asset["size"] <= MAX_ARCHIVE_BYTES
        ):
            raise BiliupRuntimeIntegrityError("Biliup asset size is invalid")
        if not _valid_sha256(asset["sha256"]):
            raise BiliupRuntimeIntegrityError("Biliup asset SHA-256 is invalid")
        _validate_initial_asset_url(version, asset["asset_name"], asset["asset_url"])
    return value


def _select_locked_asset(
    lock: dict[str, Any],
    system_name: str | None = None,
    machine_name: str | None = None,
) -> dict[str, Any]:
    platform_key = _build_platform_key(system_name, machine_name)
    asset = lock["platforms"].get(platform_key)
    if asset is None:
        raise BiliupRuntimeIntegrityError(
            f"Unsupported biliup platform: {platform_key}"
        )
    return {
        "version": lock["version"],
        "platform": platform_key,
        **asset,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_install_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliupRuntimeIntegrityError(
            "Installed biliup record is unreadable"
        ) from exc
    required = {
        "schema_version",
        "version",
        "platform",
        "asset_name",
        "asset_sha256",
        "binary_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise BiliupRuntimeIntegrityError("Installed biliup record is invalid")
    if value["schema_version"] != INSTALL_RECORD_SCHEMA:
        raise BiliupRuntimeIntegrityError(
            "Installed biliup record schema is unsupported"
        )
    if not _valid_sha256(value["asset_sha256"]) or not _valid_sha256(
        value["binary_sha256"]
    ):
        raise BiliupRuntimeIntegrityError("Installed biliup hashes are invalid")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as file_obj:
            temporary_path = Path(file_obj.name)
            json.dump(value, file_obj, ensure_ascii=False, sort_keys=True)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_redirect_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise BiliupDownloadError(
            "Biliup download redirect left the approved HTTPS hosts"
        )
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise BiliupDownloadError(
            "Biliup download redirect contains forbidden authority data"
        )


def _open_download_response(
    asset_url: str,
    downloader: Callable[..., Any],
) -> Any:
    current_url = asset_url
    for redirect_count in range(MAX_REDIRECTS + 1):
        try:
            response = downloader(
                current_url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                allow_redirects=False,
                headers={"User-Agent": "social-auto-upload"},
            )
        except requests.Timeout:
            raise BiliupDownloadError("Pinned biliup download timed out") from None
        except requests.RequestException:
            raise BiliupDownloadError("Pinned biliup download failed") from None
        status_code = int(getattr(response, "status_code", 0))
        if status_code not in REDIRECT_STATUS_CODES:
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise BiliupDownloadError(
                    "Pinned biliup download returned an HTTP error"
                ) from None
            return response

        location = response.headers.get("Location", "")
        response.close()
        if not location or redirect_count == MAX_REDIRECTS:
            raise BiliupDownloadError(
                "Pinned biliup download exceeded its redirect bound"
            )
        current_url = urljoin(current_url, location)
        _validate_redirect_url(current_url)
    raise BiliupDownloadError("Pinned biliup download exceeded its redirect bound")


def _iter_response_chunks(response: Any) -> Iterator[bytes]:
    try:
        yield from response.iter_content(chunk_size=1024 * 1024)
    except requests.Timeout:
        raise BiliupDownloadError("Pinned biliup download timed out") from None
    except requests.RequestException:
        raise BiliupDownloadError("Pinned biliup download failed") from None


def _pick_archive_member(names: list[str]) -> str:
    candidates = [
        name
        for name in names
        if PurePosixPath(name).name.lower()
        in {"biliup", "biliup.exe", "biliupr", "biliupr.exe"}
    ]
    if not candidates:
        raise BiliupRuntimeIntegrityError(
            "Pinned biliup archive does not contain a runnable executable"
        )
    candidates.sort(key=lambda item: (len(PurePosixPath(item).parts), len(item)))
    return candidates[0]


def _copy_bounded(source: Any, destination: Path) -> None:
    written = 0
    with destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_EXTRACTED_BINARY_BYTES:
                raise BiliupRuntimeIntegrityError(
                    "Extracted biliup binary exceeded its size bound"
                )
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written == 0:
        raise BiliupRuntimeIntegrityError("Extracted biliup binary is empty")


def _extract_binary(archive_path: Path, asset_name: str, destination: Path) -> None:
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            member_name = _pick_archive_member(archive.namelist())
            with archive.open(member_name) as source:
                _copy_bounded(source, destination)
        return
    if asset_name.endswith(".tar.xz"):
        with tarfile.open(archive_path, "r:xz") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            member_name = _pick_archive_member([member.name for member in members])
            member = next(item for item in members if item.name == member_name)
            source = archive.extractfile(member)
            if source is None:
                raise BiliupRuntimeIntegrityError(
                    "Unable to read the pinned biliup executable"
                )
            with source:
                _copy_bounded(source, destination)
        return
    raise BiliupRuntimeIntegrityError("Pinned biliup archive format is unsupported")


def download_biliup_asset(
    asset: dict[str, Any],
    destination: Path,
    *,
    downloader: Callable[..., Any] = requests.get,
) -> dict[str, str]:
    _validate_initial_asset_url(
        asset["version"], asset["asset_name"], asset["asset_url"]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="biliup-download-") as temp_dir:
        archive_path = Path(temp_dir) / asset["asset_name"]
        response = _open_download_response(asset["asset_url"], downloader)
        try:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError):
                    raise BiliupDownloadError(
                        "Pinned biliup download returned an invalid content length"
                    ) from None
                if declared_size != asset["size"]:
                    raise BiliupDownloadError(
                        "Pinned biliup download size differs from the reviewed lock"
                    )
            digest = hashlib.sha256()
            received = 0
            with archive_path.open("wb") as file_obj:
                for chunk in _iter_response_chunks(response):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > asset["size"]:
                        raise BiliupDownloadError(
                            "Pinned biliup download exceeded the reviewed size"
                        )
                    digest.update(chunk)
                    file_obj.write(chunk)
                file_obj.flush()
                os.fsync(file_obj.fileno())
        finally:
            response.close()
        if received != asset["size"]:
            raise BiliupDownloadError("Pinned biliup download was incomplete")
        if digest.hexdigest() != asset["sha256"]:
            raise BiliupRuntimeIntegrityError(
                "Pinned biliup download SHA-256 differs from the reviewed lock"
            )

        staged_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}-",
                suffix=".tmp",
                delete=False,
            ) as staged:
                staged_path = Path(staged.name)
            _extract_binary(archive_path, asset["asset_name"], staged_path)
            if _normalize_system(asset["platform"].split("-", 1)[0]) != "windows":
                staged_path.chmod(
                    staged_path.stat().st_mode
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )
            binary_sha256 = _sha256_file(staged_path)
            os.replace(staged_path, destination)
            staged_path = None
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
    return {
        "asset_sha256": asset["sha256"],
        "binary_sha256": binary_sha256,
    }


def read_local_biliup_version(
    system_name: str | None = None, machine_name: str | None = None
) -> str | None:
    record = _read_install_record(_build_install_record_path(system_name, machine_name))
    return None if record is None else str(record["version"])


def ensure_biliup_binary(
    force_check: bool = True,
    *,
    system_name: str | None = None,
    machine_name: str | None = None,
    lock_path: Path | None = None,
    downloader: Callable[..., Any] = requests.get,
) -> Path:
    del (
        force_check
    )  # Compatibility only: every reuse now verifies the immutable lock and binary.
    lock = load_biliup_lock(lock_path)
    asset = _select_locked_asset(lock, system_name, machine_name)
    binary_path = build_biliup_runtime_path(system_name, machine_name)
    record_path = _build_install_record_path(system_name, machine_name)
    record = _read_install_record(record_path)

    if record is not None and record["version"] == asset["version"]:
        locked_identity = {
            "platform": asset["platform"],
            "asset_name": asset["asset_name"],
            "asset_sha256": asset["sha256"],
        }
        installed_identity = {
            "platform": record["platform"],
            "asset_name": record["asset_name"],
            "asset_sha256": record["asset_sha256"],
        }
        if installed_identity != locked_identity:
            raise BiliupRuntimeIntegrityError(
                "Installed biliup version is bound to different locked asset metadata"
            )
        if binary_path.exists():
            if _sha256_file(binary_path) != record["binary_sha256"]:
                raise BiliupRuntimeIntegrityError(
                    "Installed biliup binary differs from its verified install record"
                )
            return binary_path

    hashes = download_biliup_asset(asset, binary_path, downloader=downloader)
    _atomic_write_json(
        record_path,
        {
            "schema_version": INSTALL_RECORD_SCHEMA,
            "version": asset["version"],
            "platform": asset["platform"],
            "asset_name": asset["asset_name"],
            "asset_sha256": hashes["asset_sha256"],
            "binary_sha256": hashes["binary_sha256"],
        },
    )
    return binary_path


def run_biliup_command(
    arguments: list[str], interactive: bool = False
) -> subprocess.CompletedProcess[str]:
    binary_path = ensure_biliup_binary(force_check=False)
    command = [str(binary_path), *arguments]
    if interactive:
        return subprocess.run(command, check=False)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
