"""Asset management tools — generic resource download + artifact registration.

These tools let the coder agent persist remote URLs (e.g. temporary image
links from ``generate_image``) into the workspace sandbox and declare which
files should be surfaced as previewable artifacts in the UI.

Decoupled from any specific task — works for any web project that produces
static assets (games, dashboards, landing pages, tools, etc.). No task-
specific logic lives here.

Security notes:
- ``download_asset`` explicitly calls ``is_allowed_extension`` (does NOT
  inherit ``save_output_file``'s bypass behavior, which is a known gap).
- Downloaded SVG/HTML content gets a targeted XSS scan (``<script>``,
  ``javascript:``, ``on*=``) rather than the full ``check_security`` ruleset
  — the latter false-positives on legit JSON keys / hex values.
- Size is capped at ``_MAX_DOWNLOAD_BYTES`` with streaming + timeout.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.core.langgraph.tools.sdlc_tools import (
    get_workspace_path,
    is_allowed_extension,
)
from app.core.logging import logger

_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_TIMEOUT = 30.0

# Extension → artifact type mapping for auto-inference.
_EXT_TYPE_MAP = {
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".ico": "image",
    ".mp4": "video",
    ".webm": "video",
    ".json": "json",
}

# Targeted XSS patterns for SVG/HTML. v1 coverage is intentionally minimal;
# can be extended later with more patterns (event handler attributes,
# data: URIs, CSS expression(), etc.) without changing the call site.
_XSS_PATTERNS = [
    (re.compile(r"<script\b", re.IGNORECASE), "<script> tag"),
    (re.compile(r"javascript:", re.IGNORECASE), "javascript: URL"),
    (re.compile(r"\bon\w+\s*=", re.IGNORECASE), "inline event handler"),
]


@tool
async def download_asset(
    thread_id: str,
    url: str,
    dest_path: str,
    filename: str = "",
) -> dict:
    """Download a remote resource (image, video, JSON, etc.) into the workspace.

    Use this tool to persist temporary URLs (e.g. ``generate_image`` output
    that expires in ~24h) so the asset remains accessible from the generated
    HTML. Returns both a relative ``local_path`` (for use inside HTML) and an
    ``absolute_url`` (for direct browser access).

    Args:
        thread_id: The thread ID for workspace isolation.
        url: HTTP(S) URL of the resource to download.
        dest_path: Destination directory relative to workspace root
            (e.g. "assets/images"). Created if it does not exist.
        filename: Output filename. If empty, inferred from the URL path.

    Returns:
        Dict with ``local_path``, ``absolute_url``, ``content_type``,
        ``size_bytes``, ``success``, and ``error``.

    Examples:
        download_asset(thread_id,
                       "https://dashscope-result.com/tmp/abc.png",
                       "assets/images", "snake-skin.png")
    """
    try:
        # Infer filename from URL when not provided.
        if not filename:
            url_path = urlparse(url).path
            filename = Path(url_path).name or "asset.bin"

        # Extension whitelist — MUST use is_allowed_extension explicitly,
        # NOT inherit save_output_file's bypass (known security gap).
        dest_file = Path(filename)
        if not is_allowed_extension(dest_file):
            return {
                "local_path": "",
                "absolute_url": "",
                "content_type": "",
                "size_bytes": 0,
                "success": False,
                "error": f"Extension not allowed: {dest_file.suffix}",
            }

        # Resolve destination within workspace sandbox (path-traversal safe).
        relative_path = f"{dest_path}/{filename}" if dest_path else filename
        abs_path = get_workspace_path(thread_id, relative_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream download with size cap.
        content_type = ""
        downloaded = 0
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                with open(abs_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD_BYTES:
                            abs_path.unlink(missing_ok=True)
                            return {
                                "local_path": "",
                                "absolute_url": "",
                                "content_type": content_type,
                                "size_bytes": downloaded,
                                "success": False,
                                "error": f"Exceeded {_MAX_DOWNLOAD_BYTES} bytes limit",
                            }
                        f.write(chunk)

        # Targeted XSS scan for SVG/HTML only — NOT full check_security
        # (which false-positives on legit JSON keys / hex values).
        ext = dest_file.suffix.lower()
        if ext in {".svg", ".html", ".htm"}:
            xss_issues = _scan_svg_xss(abs_path.read_text(encoding="utf-8"))
            if xss_issues:
                abs_path.unlink(missing_ok=True)
                return {
                    "local_path": "",
                    "absolute_url": "",
                    "content_type": content_type,
                    "size_bytes": downloaded,
                    "success": False,
                    "error": f"XSS pattern detected: {xss_issues[0]}",
                }

        preview_url = f"{settings.WORKSPACE_PREVIEW_URL_PREFIX}/{thread_id}/{relative_path}"
        logger.info(
            "asset_downloaded",
            thread_id=thread_id,
            url=url,
            path=relative_path,
            size=downloaded,
        )
        return {
            "local_path": relative_path,
            "absolute_url": preview_url,
            "content_type": content_type,
            "size_bytes": downloaded,
            "success": True,
            "error": None,
        }
    except Exception as e:
        logger.exception("asset_download_failed", thread_id=thread_id, url=url, error=str(e))
        return {
            "local_path": "",
            "absolute_url": "",
            "content_type": "",
            "size_bytes": 0,
            "success": False,
            "error": str(e),
        }


@tool
def register_artifact(
    thread_id: str,
    name: str,
    path: str,
    artifact_type: str = "auto",
    description: str = "",
) -> dict:
    """Register a workspace file as a previewable artifact.

    Use this tool AFTER writing/persisting files to declare which ones the
    user should be able to preview. Call once per artifact; the UI will
    render a preview panel listing all registered artifacts. If the task
    produces no previewable output (e.g. a refactor), do not call this tool.

    Args:
        thread_id: The thread ID for workspace isolation.
        name: Human-readable name (e.g. "Snake Game", "API Docs",
            "Sales Dashboard"). Shown as the preview button label.
        path: Relative path to the file within the workspace.
        artifact_type: One of "auto" (infer from extension), "html",
            "image", "video", "text", "json", "other".
        description: Optional description shown under the name.

    Returns:
        Dict with ``success``, ``type``, ``preview_url``, and ``error``.
    """
    try:
        abs_path = get_workspace_path(thread_id, path)
        if not abs_path.exists():
            return {"success": False, "type": "", "preview_url": "", "error": f"File not found: {path}"}

        resolved_type = _resolve_artifact_type(artifact_type, abs_path)
        preview_url = f"{settings.WORKSPACE_PREVIEW_URL_PREFIX}/{thread_id}/{path}"

        # Each artifact = independent JSON file under .artifacts/ dir.
        # Avoids read-modify-write race on a single manifest JSON.
        artifacts_dir = get_workspace_path(thread_id, ".artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize name → safe filename (alnum + -_).
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:50]
        artifact_file = artifacts_dir / f"{safe_name}.json"

        artifact_data = {
            "name": name,
            "path": path,
            "type": resolved_type,
            "description": description,
            "preview_url": preview_url,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        artifact_file.write_text(
            json.dumps(artifact_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "artifact_registered",
            thread_id=thread_id,
            name=name,
            path=path,
            type=resolved_type,
        )
        return {
            "success": True,
            "type": resolved_type,
            "preview_url": preview_url,
            "error": None,
        }
    except Exception as e:
        logger.exception("artifact_register_failed", thread_id=thread_id, name=name, error=str(e))
        return {"success": False, "type": "", "preview_url": "", "error": str(e)}


def _scan_svg_xss(content: str) -> list[str]:
    """Targeted XSS scan for SVG/HTML — blocks script injection only.

    Intentionally minimal (v1): avoids ``check_security`` false positives on
    legit JSON keys / hex values. Coverage can be extended later without
    changing the call site.
    """
    issues: list[str] = []
    for pattern, label in _XSS_PATTERNS:
        if pattern.search(content):
            issues.append(label)
    return issues


def _resolve_artifact_type(artifact_type: str, abs_path: Path) -> str:
    """Infer artifact type from extension when ``artifact_type='auto'``."""
    if artifact_type != "auto":
        return artifact_type
    return _EXT_TYPE_MAP.get(abs_path.suffix.lower(), "text")


# Exported for the registry builder — mirrors the media_tools_list pattern.
asset_tools_list = [download_asset, register_artifact]
