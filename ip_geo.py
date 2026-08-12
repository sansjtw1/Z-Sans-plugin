"""
IP Geo-Location Plugin for Z-Sans
=================================
Resolve the geographic location of every discovered IP asset via the free
ip-api.com endpoint and attach it to the asset's ``properties["geo"]``, so it
flows through the JSON/CSV/GraphML exports. A per-run summary report is also
written next to the other run outputs.

Manifest:
    name: ip_geo
    version: 1.0.0
    description: Resolve IP geolocation via ip-api.com and annotate assets
    author: Z-Sans SansJtw

Supported asset types:
    - ip      (IPv4 / IPv6 addresses)

Configuration (breeding-config.yaml):
    plugins:
      ip_geo:
        enabled: true               # master switch (default: true)
        timeout: 8                  # per-request timeout in seconds (default: 8)
        batch_size: 45              # ip-api.com free tier allows 45 req/min (default: 45)
        fields: status,country,regionName,city,isp,org,as,lat,lon,timezone
        skip_private: true          # do not query RFC1918 / private IPs (default: true)
        https: false                # use https:// (free tier is HTTP-only; default: false)

Environment variables:
    IP_GEO_TIMEOUT
    IP_GEO_BATCH_SIZE
    IP_GEO_SKIP_PRIVATE
"""

import ipaddress
import logging
import os
import threading
import time

import requests

logger = logging.getLogger("zsans.plugin.ip_geo")

__manifest__ = {
    "name": "ip_geo",
    "version": "1.0.0",
    "description": "Resolve IP geolocation via ip-api.com and annotate assets",
    "author": "Z-Sans SansJtw",
}

# Module-level state (cleared on each scan)
_engine = None
_config = {}
_stats = {
    "queried": 0,
    "success": 0,
    "failed": 0,
    "skipped_private": 0,
}
_geo_results = {}          # ip -> geo dict
_state_lock = threading.Lock()
_batch_lock = threading.Lock()
_batch_ts = 0.0
_batch_count = 0


def _resolve_config(engine):
    """Read configuration from YAML + environment variables."""
    global _config
    plugin_cfg = {}
    if engine and hasattr(engine, "config"):
        pcfg = engine.config.get("plugins", {})
        if isinstance(pcfg, dict):
            plugin_cfg = pcfg.get("ip_geo", {}) or {}

    _config = {
        "enabled": plugin_cfg.get("enabled", True),
        "timeout": float(os.environ.get("IP_GEO_TIMEOUT", plugin_cfg.get("timeout", 8))),
        "batch_size": int(os.environ.get("IP_GEO_BATCH_SIZE", plugin_cfg.get("batch_size", 45))),
        "fields": plugin_cfg.get(
            "fields", "status,country,regionName,city,isp,org,as,lat,lon,timezone"
        ),
        "skip_private": bool(os.environ.get("IP_GEO_SKIP_PRIVATE", plugin_cfg.get("skip_private", True))),
        "https": bool(plugin_cfg.get("https", False)),
    }
    logger.debug("ip_geo config: %s", _config)


def _is_private_ip(ip):
    """Return True for private / reserved / link-local addresses."""
    try:
        addr = ipaddress.ip_address(ip)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )
    except ValueError:
        return True


def _respect_rate_limit():
    """Throttle requests to the ip-api.com free-tier window (45 req/min).

    The sleep is performed *outside* the lock so one throttled thread does not
    block all concurrent workers waiting on ``_batch_lock``.
    """
    global _batch_ts, _batch_count
    with _batch_lock:
        now = time.time()
        window = 60.0
        limit = max(1, _config.get("batch_size", 45))
        if now - _batch_ts >= window:
            _batch_ts = now
            _batch_count = 0
        if _batch_count >= limit:
            wait = window - (now - _batch_ts)
            _batch_count = 0
            _batch_ts = time.time()
        else:
            wait = 0.0
        _batch_count += 1

    if wait > 0:
        logger.debug("ip_geo rate limit: sleeping %.1fs", wait)
        time.sleep(wait)


def _query_geo(ip):
    """Query ip-api.com for a single IP. Returns a dict or None.

    Note: the free tier of ip-api.com serves requests over HTTP only and
    returns 403 for HTTPS, so HTTP is the default transport.
    """
    scheme = "https" if _config.get("https") else "http"
    url = f"{scheme}://ip-api.com/json/{ip}"
    try:
        _respect_rate_limit()
        r = requests.get(
            url,
            timeout=_config.get("timeout", 8),
            params={"fields": _config.get("fields")},
        )
        if r.status_code == 429:
            logger.warning("ip_geo rate limited (429); skipping %s", ip)
            return None
        if r.status_code == 403 and scheme == "https":
            # Free tier is HTTP-only; retry over HTTP
            logger.debug("ip_geo got 403 over https for %s, retrying over http", ip)
            return _query_geo_http(ip)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "success" or ("country" in data and data.get("country")):
            return data
        logger.debug("ip_geo query returned no data for %s: %s", ip, data)
        return None
    except requests.exceptions.Timeout:
        logger.debug("ip_geo query timed out for %s", ip)
        return None
    except requests.exceptions.SSLError as e:
        # Free tier is HTTP-only; fall back to http on SSL errors
        if scheme == "https":
            logger.debug("ip_geo SSL error for %s, retrying over http: %s", ip, e)
            return _query_geo_http(ip)
        return None
    except Exception as e:
        logger.debug("ip_geo query failed for %s: %s", ip, e)
        return None


def _query_geo_http(ip):
    """Query ip-api.com over plain HTTP (free tier)."""
    try:
        _respect_rate_limit()
        r = requests.get(
            f"http://ip-api.com/json/{ip}",
            timeout=_config.get("timeout", 8),
            params={"fields": _config.get("fields")},
        )
        if r.status_code == 200 and r.json().get("status") == "success":
            return r.json()
    except Exception as e:
        logger.debug("ip_geo http retry failed for %s: %s", ip, e)
    return None


def _process_ip_asset(asset):
    """Annotate one IP asset with geolocation data."""
    global _stats, _geo_results
    if not _config.get("enabled", True):
        return
    if asset is None or getattr(asset, "type", "") != "ip":
        return
    if "geo" in getattr(asset, "properties", {}):
        return

    ip = (getattr(asset, "value", "") or "").strip()
    if not ip:
        return
    if _config.get("skip_private", True) and _is_private_ip(ip):
        with _state_lock:
            _stats["skipped_private"] += 1
        return

    with _state_lock:
        _stats["queried"] += 1

    geo = _query_geo(ip)
    if geo:
        try:
            asset.properties["geo"] = geo
        except Exception as e:
            logger.debug("ip_geo could not annotate %s: %s", ip, e)
        with _state_lock:
            _stats["success"] += 1
            _geo_results[ip] = geo
        logger.info("ip_geo: %s -> %s, %s", ip, geo.get("city", ""), geo.get("country", ""))
    else:
        with _state_lock:
            _stats["failed"] += 1


# ─────────────────────────────────────────────
# Event handlers
# ─────────────────────────────────────────────

def on_scan_started(engine):
    """Initialize config and reset per-scan state."""
    global _engine, _stats, _geo_results
    _engine = engine
    _resolve_config(engine)
    with _state_lock:
        _stats = {"queried": 0, "success": 0, "failed": 0, "skipped_private": 0}
        _geo_results = {}


def on_asset_discovered(asset, source):
    """Look up geolocation for every IP asset as it is discovered."""
    _process_ip_asset(asset)


def on_scan_completed(engine):
    """Write a summary geolocation report next to the other run outputs."""
    if not _engine:
        return
    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not outdir:
        return
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "ip_geo_report.json")
        with _state_lock:
            stats = dict(_stats)
            results = dict(_geo_results)
        import json as _json
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({
                "plugin": "ip_geo",
                "version": __manifest__["version"],
                "stats": stats,
                "results": results,
            }, f, ensure_ascii=False, indent=2)
        logger.info("ip_geo summary: %s", path)
    except Exception as e:
        logger.warning("ip_geo summary write failed: %s", e)
