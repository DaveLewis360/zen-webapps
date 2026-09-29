"""Small dependency-free PEP 517 wheel/sdist backend for this pure-Python project."""

from __future__ import annotations

import base64
import csv
from email.message import Message
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

NAME = "zen_app_runtime"
VERSION = "0.1.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"
ROOT = Path(__file__).resolve().parents[2]
BROWSER_ADAPTER_RESOURCE = Path("browser_adapter_bundle")
RUNTIME_CANDIDATE_RESOURCE = Path("runtime_candidates")
CHROMIUM_SHELL_RESOURCE = Path("chromium_shell")
APP_MODE_PACKAGE = Path("appmode")


def _package_sources(root: Path) -> list[Path]:
    package = root / "src" / NAME
    sources = list(package.glob("*.py"))
    adapter = package / BROWSER_ADAPTER_RESOURCE
    if adapter.is_dir():
        sources.extend(path for path in adapter.rglob("*") if path.is_file())
    candidate_locks = package / RUNTIME_CANDIDATE_RESOURCE
    if candidate_locks.is_dir():
        sources.extend(candidate_locks.glob("*.lock.json"))
    chromium_shell = package / CHROMIUM_SHELL_RESOURCE
    if chromium_shell.is_dir():
        sources.extend(path for path in chromium_shell.rglob("*") if path.is_file())
    app_mode = package / APP_MODE_PACKAGE
    if app_mode.is_dir():
        sources.extend(
            path
            for path in app_mode.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")
        )
    return sorted(sources)


def _metadata() -> bytes:
    message = Message()
    message["Metadata-Version"] = "2.3"
    message["Name"] = "zen-app-runtime"
    message["Version"] = VERSION
    message["Summary"] = "Manifest and app-definition manager for isolated Zen site-specific apps"
    message["Requires-Python"] = ">=3.11"
    return message.as_bytes()


def _wheel() -> bytes:
    return b"Wheel-Version: 1.0\nGenerator: zen-app-runtime\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _entry_points() -> bytes:
    return b"[console_scripts]\nzen-app = zen_app_runtime.cli:main\n"


def _dist_info_files() -> dict[str, bytes]:
    return {
        f"{DIST_INFO}/METADATA": _metadata(),
        f"{DIST_INFO}/WHEEL": _wheel(),
        f"{DIST_INFO}/entry_points.txt": _entry_points(),
    }


def _write_metadata(directory: str) -> str:
    target = Path(directory) / DIST_INFO
    target.mkdir(parents=True, exist_ok=True)
    for archive_name, content in _dist_info_files().items():
        (target / Path(archive_name).name).write_bytes(content)
    return DIST_INFO


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    return _write_metadata(metadata_directory)


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    target = Path(wheel_directory) / filename
    files = _dist_info_files()
    for source in _package_sources(ROOT):
        relative = source.relative_to(ROOT / "src" / NAME)
        files[f"{NAME}/{relative.as_posix()}"] = source.read_bytes()

    records: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name in sorted(files):
            content = files[archive_name]
            info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            records.append((archive_name, f"sha256={digest}", str(len(content))))
        record_name = f"{DIST_INFO}/RECORD"
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerows([*records, (record_name, "", "")])
        info = zipfile.ZipInfo(record_name, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, stream.getvalue().encode())
    return filename


def _build_sdist(root: Path, output) -> None:
    """Write a normalized source archive for ``root`` to a binary stream."""
    prefix = f"zen_app_runtime-{VERSION}"
    included = [root / "pyproject.toml", root / "README.md"]
    included.extend(_package_sources(root))
    included.extend(sorted((root / "tests").glob("test_*.py")))
    included.extend(sorted((root / "docs").glob("*.md")))
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            package_info = _metadata()
            info = tarfile.TarInfo(name=f"{prefix}/PKG-INFO")
            info.size = len(package_info)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(package_info))
            for source in included:
                info = archive.gettarinfo(str(source), arcname=f"{prefix}/{source.relative_to(root)}")
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with source.open("rb") as stream:
                    archive.addfile(info, stream)


def build_sdist(sdist_directory: str, config_settings=None) -> str:
    filename = f"zen_app_runtime-{VERSION}.tar.gz"
    target = Path(sdist_directory) / filename
    with target.open("wb") as raw:
        _build_sdist(ROOT, raw)
    return filename
