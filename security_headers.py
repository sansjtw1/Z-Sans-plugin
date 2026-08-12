"""
HTTP Security Headers Plugin for Z-Sans
=======================================
Probe every discovered URL asset with a lightweight HTTP request, inspect the
response headers, and annotate the asset with a security-header scorecard under
``asset.properties["http_security"]`` so it flows through JSON/CSV/GraphML
exports. A per-run summary report is written next to the other run outputs.

Manifest:
    name: security_headers
    version: 1.0.0
    description: Grade HTTP security headers for discovered URL assets
    author: Z-Sans Contributors

Supported asset types:
    - url

Configuration (breeding-config.yaml):
    plugins:
      security_headers:
        enabled: true               # master switch (default: true)
        timeout: 8                  # per-request timeout in seconds (default: 8)
        max_workers: 10             # concurrent probes (default: 10)
        max_probes: 500             # cap on number of URLs probed per run (default: 500)
        method: HEAD                # HEAD or GET (default: HEAD)
        verify_ssl: false           # skip TLS verification (default: false)

Headers checked:
    Strict-Transport-Security (HSTS)
    Content-Security-Policy     (CSP)
    X-Frame-Options
    X-Content-Type-Options
    Referrer-Policy
    Permissions-Policy
    X-Permitted-Cross-Domain-Policies
    Cross-Origin-Opener-Policy
    Cross-Origin-Resource-Policy
    Cross-Origin-Embedder-Policy
"""

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger("zsans.plugin.security_headers")

__manifest__ = {
    "name": "security_headers",
    "version": "1.0.0",
    "description": "Grade HTTP security headers for discovered URL assets",
    "author": "Z-Sans Contributors",
}

# Headers we care about and a short human label for the report.
_WANTED_HEADERS = {
    "Strict-Transport-Security": "HSTS",
    "Content-Security-Policy": "CSP",
    "X-Frame-Options": "XFO",
    "X-Content-Type-Options": "XCTO",
    "Referrer-Policy": "Referrer",
    "Permissions-Policy": "Permissions",
    "X-Permitted-Cross-Domain-Policies": "X-PCDP",
    "Cross-Origin-Opener-Policy": "COOP",
    "Cross-Origin-Resource-Policy": "CORP",
    "Cross-Origin-Embedder-Policy": "COEP",
}

_MISSING = "missing"

# Module-level state
_engine = None
_config = {}
_stats = {
    "probed": 0,
    "success": 0,
    "failed": 0,
    "missing_any": 0,
}
_results = {}          # uid -> {"url":..., "grade":..., "headers":..., "status":...}
_state_lock = threading.Lock()


def _resolve_config(engine):
    """Read configuration from YAML + environment variables."""
    global _config
    plugin_cfg = {}
    if engine and hasattr(engine, "config"):
        pcfg = engine.config.get("plugins", {})
        if isinstance(pcfg, dict):
            plugin_cfg = pcfg.get("security_headers", {}) or {}
    _config = {
        "enabled": plugin_cfg.get("enabled", True),
        "timeout": float(plugin_cfg.get("timeout", 8)),
        "max_workers": int(plugin_cfg.get("max_workers", 10)),
        "max_probes": int(plugin_cfg.get("max_probes", 500)),
        "method": str(plugin_cfg.get("method", "HEAD")).upper(),
        "verify_ssl": bool(plugin_cfg.get("verify_ssl", False)),
    }
    if _config["method"] not in ("HEAD", "GET"):
        _config["method"] = "HEAD"
    logger.debug("security_headers config: %s", _config)


def _collect_url_assets(engine):
    """Return a bounded list of (uid, url) for URL assets in the graph."""
    assets = []
    graph = getattr(engine, "asset_graph", None)
    if graph is None:
        return assets
    try:
        nodes = getattr(graph, "nodes", {}) or {}
    except Exception:
        return assets
    for uid, asset in nodes.items():
        if getattr(asset, "type", "") != "url":
            continue
        value = (getattr(asset, "value", "") or "").strip()
        if value.startswith(("http://", "https://")):
            assets.append((uid, value))
        if len(assets) >= _config.get("max_probes", 500):
            break
    return assets


def _grade(header_map):
    """Compute a simple A-F grade based on how many key headers are present."""
    present = [k for k in _WANTED_HEADERS if header_map.get(k) not in (None, "")]
    ratio = len(present) / len(_WANTED_HEADERS)
    if ratio >= 0.8:
        return "A"
    if ratio >= 0.6:
        return "B"
    if ratio >= 0.4:
        return "C"
    if ratio >= 0.2:
        return "D"
    return "F"


def _probe_url(url):
    """Return (url, headers_map, http_status) for a single URL."""
    method = _config.get("method", "HEAD")
    try:
        # 复用全局 http_session，继承代理 / User-Agent / SSL 校验等引擎级配置
        from core.zsans_engine import http_session
        resp = http_session.request(
            method,
            url,
            timeout=_config.get("timeout", 8),
            verify=_config.get("verify_ssl", False),
            allow_redirects=True,
        )
    except Exception as e:
        logger.debug("security_headers probe failed for %s: %s", url, e)
        return url, None, None

    header_map = {k: resp.headers.get(k) for k in _WANTED_HEADERS}
    return url, header_map, resp.status_code


def _run_probes(urls):
    """Probe a list of (uid, url) tuples concurrently."""
    with ThreadPoolExecutor(max_workers=_config.get("max_workers", 10)) as pool:
        futures = {pool.submit(_probe_url, url): uid for uid, url in urls}
        for future in as_completed(futures):
            uid = futures[future]
            url, header_map, status = future.result()
            with _state_lock:
                _stats["probed"] += 1
                if header_map is None:
                    _stats["failed"] += 1
                    continue
                _stats["success"] += 1
                grade = _grade(header_map)
                if grade in ("D", "F"):
                    _stats["missing_any"] += 1
                _results[uid] = {
                    "url": url,
                    "status": status,
                    "grade": grade,
                    "headers": header_map,
                }


def _annotate_assets(engine):
    """Attach the security-header scorecard to each URL asset in the graph."""
    graph = getattr(engine, "asset_graph", None)
    if graph is None:
        return
    nodes = getattr(graph, "nodes", {}) or {}
    for uid, asset in nodes.items():
        if uid in _results and getattr(asset, "type", "") == "url":
            try:
                asset.properties["http_security"] = {
                    "grade": _results[uid]["grade"],
                    "status": _results[uid]["status"],
                    "headers": _results[uid]["headers"],
                }
            except Exception as e:
                logger.debug("security_headers annotate failed for %s: %s", uid, e)


# ─────────────────────────────────────────────
# Event handlers
# ─────────────────────────────────────────────

def on_scan_started(engine):
    """Initialize config and reset per-scan state."""
    global _engine, _stats, _results
    _engine = engine
    _resolve_config(engine)
    with _state_lock:
        _stats = {"probed": 0, "success": 0, "failed": 0, "missing_any": 0}
        _results = {}


def on_scan_completed(engine):
    """Probe all discovered URL assets and write a summary report."""
    if not _config.get("enabled", True):
        return
    urls = _collect_url_assets(engine)
    if not urls:
        logger.info("security_headers: no URL assets to probe")
        return
    logger.info("security_headers: probing %d URL assets", len(urls))
    _run_probes(urls)
    _annotate_assets(engine)

    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not outdir:
        return
    try:
        os.makedirs(outdir, exist_ok=True)
        with _state_lock:
            stats = dict(_stats)
            results = {uid: dict(v) for uid, v in _results.items()}
        import json as _json
        report_path = os.path.join(outdir, "security_headers_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            _json.dump({
                "plugin": "security_headers",
                "version": __manifest__["version"],
                "stats": stats,
                "results": results,
            }, f, ensure_ascii=False, indent=2)

        # Human-readable table
        lines = []
        lines.append("HTTP Security Headers Report")
        lines.append("=" * 60)
        lines.append("Stats: {success} ok / {failed} failed / {probed} probed".format(**stats))
        lines.append("")
        header_row = "{:<8} {:<10} {:<6} {}".format("Grade", "Status", "Type", "URL")
        lines.append(header_row)
        lines.append("-" * 60)
        for uid, r in sorted(results.items(), key=lambda kv: (kv[1]["grade"], kv[0])):
            url = r["url"]
            lines.append("{:<8} {:<10} {:<6} {}".format(r["grade"], r.get("status"), r["url"].split(":")[0], url))
        txt_path = os.path.join(outdir, "security_headers_report.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        logger.info("security_headers report: %s / %s", report_path, txt_path)
    except Exception as e:
        logger.warning("security_headers summary write failed: %s", e)
