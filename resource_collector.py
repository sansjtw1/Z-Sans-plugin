"""
Resource Collector Plugin for Z-Sans
====================================
Downloads all discovered file resources (including JS, CSS, images, documents,
etc.) to a local directory under the engine's run_dir.

Manifest:
    name: resource_collector
    version: 1.0.0
    description: Download discovered file/JS resources to local disk
    author: Z-Sans SansJtw

Supported asset types:
    - js       (JavaScript files)
    - url      (Generic URLs that point to downloadable files)
    - file     (Explicit file-type assets)

Configuration (breeding-config.yaml):
    plugins:
      resource_collector:
        download_dir: "resources"       # subdir under run_dir (default: "resources")
        timeout: 20                     # request timeout in seconds (default: 20)
        max_file_size: 52428800         # 50 MB limit per file (default: 50 MB)
        skip_extensions: []             # e.g. [".mp4", ".iso"] to skip large binaries
        user_agent: "Z-Sans-Resource-Collector/1.0"

Environment variables:
    RESOURCE_COLLECTOR_DOWNLOAD_DIR
    RESOURCE_COLLECTOR_TIMEOUT
    RESOURCE_COLLECTOR_MAX_FILE_SIZE
"""

import os
import re
import logging
import hashlib
import threading
import mimetypes
from urllib.parse import urlparse, unquote

import requests

logger = logging.getLogger("zsans.plugin.resource_collector")

# ─────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────
__manifest__ = {
    "name": "resource_collector",
    "version": "1.0.0",
    "description": "Download discovered file/JS resources to local disk",
    "author": "Z-Sans SansJtw",
}

# ─────────────────────────────────────────────
# Module-level state (cleared on each scan)
# ─────────────────────────────────────────────
_engine = None
_download_dir = None
_config = {}
_stats = {
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "total_bytes": 0,
}
_state_lock = threading.Lock()

# 失败原因分类统计（key=原因, value=[url, ...]）
_failures = {}

# Extensions we always consider "downloadable resources"
_RESOURCE_EXTENSIONS = {
    # JavaScript
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    # Stylesheets
    ".css", ".scss", ".less",
    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv", ".json", ".xml",
    # Web artifacts
    ".html", ".htm", ".svg", ".webmanifest", ".map",
    # Archives
    ".zip", ".tar", ".gz", ".rar", ".7z",
    # Images (often useful for OSINT)
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".avif",
    # Media
    ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".flac",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Config / data files
    ".yaml", ".yml", ".ini", ".conf", ".env", ".sql",
}

# Binary / very large types we might want to skip by default
_SKIP_BY_DEFAULT = {".mp4", ".mp3", ".webm", ".ogg", ".wav", ".flac", ".avi", ".mov", ".mkv"}

# Thread-safe file-write lock (prevents partial-file races on same filename)
_file_lock = threading.Lock()


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _resolve_config(engine):
    """Read configuration from YAML + environment variables."""
    global _config
    plugin_cfg = {}
    if engine and hasattr(engine, "config"):
        plugin_cfg = engine.config.get("plugins", {}).get("resource_collector", {})

    _config = {
        "download_dir": os.environ.get(
            "RESOURCE_COLLECTOR_DOWNLOAD_DIR",
            plugin_cfg.get("download_dir", "resources"),
        ),
        "timeout": int(os.environ.get(
            "RESOURCE_COLLECTOR_TIMEOUT",
            plugin_cfg.get("timeout", 20),
        )),
        "max_file_size": int(os.environ.get(
            "RESOURCE_COLLECTOR_MAX_FILE_SIZE",
            plugin_cfg.get("max_file_size", 50 * 1024 * 1024),
        )),
        "skip_extensions": set(plugin_cfg.get("skip_extensions", [])) | _SKIP_BY_DEFAULT,
        "user_agent": plugin_cfg.get(
            "user_agent",
            "Z-Sans-Resource-Collector/1.0 (+https://example.com/bot)",
        ),
        "verify_ssl": plugin_cfg.get("verify_ssl", True),
    }
    logger.debug("resource_collector config: %s", _config)


def _prepare_download_dir(engine):
    """Create the download directory under engine.run_dir (or output_dir fallback)."""
    global _download_dir
    handler = getattr(engine, "output_handler", None)
    base = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not base:
        base = os.getcwd()
        logger.warning("No output_handler found; falling back to CWD: %s", base)

    _download_dir = os.path.join(base, _config["download_dir"])
    os.makedirs(_download_dir, exist_ok=True)
    logger.info("Resource collector download dir: %s", _download_dir)


def _is_downloadable(asset):
    """Decide whether an asset represents a downloadable file resource."""
    if not asset:
        return False

    atype = getattr(asset, "type", "")
    value = getattr(asset, "value", "") or ""
    url = str(value).strip()

    if not url.startswith(("http://", "https://")):
        return False

    # 只有这几类资产会携带文件资源 URL
    if atype not in ("js", "url", "file"):
        return False

    # 排除查询/锚点后取路径扩展名
    try:
        path = urlparse(url).path
        if path == "" or path == "/":
            return False
        ext = os.path.splitext(path)[1].lower()
    except Exception:
        ext = ""

    if not ext:
        # 无扩展名但可能是 JS/JSON 等无后缀资源，交由下载时按 Content-Type 判定
        return atype in ("js", "file")

    if ext in _config.get("skip_extensions", set()):
        return False

    return ext in _RESOURCE_EXTENSIONS or atype in ("js", "file")


def _safe_filename(url):
    """Build a filesystem-safe filename from a URL, preserving the extension.

    Format: <host>__<path_with_hash_prefix> so the original extension survives
    (e.g. ``cdn.jsdelivr.net__npm__lodash__4.17.10__<hash>__lodash.min.js``).
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_")
    path = unquote(parsed.path).strip("/")
    if not path:
        path = "index"

    # 拆出扩展名，hash 放在扩展名之前，保证扩展名保留在文件名末尾
    ext = os.path.splitext(path)[1]
    stem = path[:-len(ext)] if ext else path
    # 转义非法文件名字符
    stem_safe = re.sub(r'[^A-Za-z0-9._\-]', '_', stem)
    stem_safe = stem_safe.strip('_') or 'index'
    if len(stem_safe) > 100:
        stem_safe = stem_safe[-100:]
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    return f"{host}__{stem_safe}__{digest}{ext}"


def _download_one(url):
    """Download a single URL to disk. Returns (status, path_or_reason)."""
    global _stats
    if not url.startswith(("http://", "https://")):
        return ("skipped", "not-http")

    try:
        cfg = _config
        with requests.Session() as s:
            s.headers.update({
                "User-Agent": cfg["user_agent"],
                "Accept": "*/*",
            })
            with s.get(
                url,
                timeout=cfg["timeout"],
                stream=True,
                verify=cfg["verify_ssl"],
                allow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    _record_failure(url, f"http-{resp.status_code}")
                    return ("failed", f"http-{resp.status_code}")

                # 内容长度判定（超过上限直接跳过）
                clen = resp.headers.get("Content-Length")
                if clen:
                    try:
                        if int(clen) > cfg["max_file_size"]:
                            return ("skipped", "too-large")
                    except ValueError:
                        pass

                # 写入
                with _file_lock:
                    fname = os.path.join(_download_dir, _safe_filename(url))
                    tmp = fname + ".part"
                    bytes_written = 0
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                bytes_written += len(chunk)
                                if bytes_written > cfg["max_file_size"]:
                                    f.close()
                                    os.remove(tmp)
                                    return ("skipped", "too-large")
                    os.replace(tmp, fname)

                with _state_lock:
                    _stats["downloaded"] += 1
                    _stats["total_bytes"] += bytes_written
                logger.info("Resource downloaded: %s -> %s (%d bytes)", url, fname, bytes_written)
                return ("downloaded", fname)
    except requests.exceptions.Timeout:
        _record_failure(url, "timeout")
        return ("failed", "timeout")
    except requests.exceptions.SSLError as e:
        _record_failure(url, "ssl-error")
        return ("failed", "ssl-error")
    except requests.exceptions.ConnectionError as e:
        _record_failure(url, "connection-error")
        return ("failed", "connection-error")
    except Exception as e:
        _record_failure(url, str(e)[:80])
        logger.debug("Resource download failed: %s (%s)", url, e)
        return ("failed", str(e)[:80])


def _record_failure(url, reason):
    """Record a failed URL under its failure reason, thread-safe."""
    with _state_lock:
        _failures.setdefault(reason, []).append(url)


def _download_asset(asset):
    """Entry point called from the event handler."""
    global _stats
    if _download_dir is None:
        return
    if not _is_downloadable(asset):
        return

    url = (getattr(asset, "value", "") or "").strip()
    status, info = _download_one(url)
    if status == "skipped":
        with _state_lock:
            _stats["skipped"] += 1
        logger.debug("Resource skipped: %s (%s)", url, info)
    elif status == "failed":
        with _state_lock:
            _stats["failed"] += 1
        logger.debug("Resource failed: %s (%s)", url, info)


# ─────────────────────────────────────────────
# Event handlers
# ─────────────────────────────────────────────

def on_scan_started(engine):
    """Initialize config + download dir at the beginning of a scan."""
    global _engine, _download_dir, _stats, _failures
    _engine = engine
    _resolve_config(engine)
    _prepare_download_dir(engine)
    with _state_lock:
        _stats = {"downloaded": 0, "skipped": 0, "failed": 0, "total_bytes": 0}
        _failures = {}


def on_asset_scanned(asset, new_assets):
    """Download each scanned asset that is a file resource."""
    _download_asset(asset)


def on_scan_completed(engine):
    """Write a small summary report next to the other run outputs."""
    if _download_dir is None:
        return
    try:
        with _state_lock:
            stats = dict(_stats)
            failures = {k: v[:50] for k, v in _failures.items()}
        summary_path = os.path.join(_download_dir, "resource_collector_report.json")
        import json as _json
        with open(summary_path, "w", encoding="utf-8") as f:
            _json.dump({
                "plugin": "resource_collector",
                "version": __manifest__["version"],
                "stats": stats,
                "download_dir": _download_dir,
                "failures": failures,
            }, f, ensure_ascii=False, indent=2)
        logger.info("Resource collector summary: %s", summary_path)
    except Exception as e:
        logger.warning("Resource collector summary write failed: %s", e)