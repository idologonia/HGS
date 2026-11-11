"""Download cloudflared from Cloudflare's official GitHub releases (only when the user asks).

The SHA-256 published by GitHub for the release asset is verified before the file is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import tempfile
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
USER_AGENT = "HGS-cloudflared-downloader"


class DownloadError(RuntimeError):
    pass


def asset_name() -> str:
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("amd64", "x86_64") else "arm64" if machine in ("arm64", "aarch64") else "386"
    if sys.platform == "win32":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform.startswith("linux"):
        return f"cloudflared-linux-{arch}"
    raise DownloadError("automatic download is supported on Windows and Linux; install cloudflared manually")


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https URLs)
        return r.read()


def download(dest_dir: Path) -> dict:
    """Blocking; run in a thread. Returns {path, version, sha256}."""
    name = asset_name()
    try:
        release = json.loads(_get(API))
    except Exception as exc:
        raise DownloadError(f"cannot read the latest release from GitHub: {exc}") from exc
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise DownloadError(f"release {release.get('tag_name')} has no file {name}")
    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise DownloadError("GitHub did not publish a SHA-256 checksum for this file; download it manually")
    expected = digest.split(":", 1)[1].lower()
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/cloudflare/cloudflared/"):
        raise DownloadError("unexpected download location")
    try:
        data = _get(url, timeout=300)
    except Exception as exc:
        raise DownloadError(f"download failed: {exc}") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise DownloadError("checksum mismatch: the downloaded file was NOT saved")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if sys.platform != "win32":
        os.chmod(tmp, 0o755)
    os.replace(tmp, final)
    return {"path": str(final), "version": release.get("tag_name"), "sha256": actual}
