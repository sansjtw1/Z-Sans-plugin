"""domain_whois — enrich domain assets with WHOIS registration data via the
free, global RDAP service (rdap.org).

For every ``domain`` asset found during a scan this plugin queries the RDAP
bootstrap service (freely available, no API key, one JSON endpoint for every
registry) and summarizes the authoritative registration data — registrar,
registrant organization / email / phone, registration & expiration dates,
status flags, name servers and DNSSEC — into
``asset.properties["whois"]`` so it flows into the exported JSON/CSV reports.

International by design: RDAP is the standard served by every gTLD/country-code
registry and the modern wholesale replacement for the whois protocol; the
plugin's entire surface (config, logs, output keys) is English.

Heuristics to stay quiet on big scans:

    * ICANN-only gTLD queries are de-duplicated by SLD — ``example.com`` and
      ``www.example.com`` resolve to one registry lookup for ``example.com``,
      and the result is cached and reused.
    * a hard query budget (``max_queries``, default 200) per scan prevents
      runaway API traffic.
    * results are cached per registrable domain.
"""

import datetime
import json
import logging
import os
import re
import threading

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("zsans.plugin.domain_whois")

__manifest__ = {
    "name": "domain_whois",
    "version": "1.0.0",
    "description": "Enrich domain assets with WHOIS registration data via free global RDAP (registrar / dates / status / NS / DNSSEC)",
    "author": "Z-Sans Contributors",
}

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_TIMEOUT = 20

# 默认查询源（按序逐个尝试，直到成功）。
# {domain} 会被替换为注册级域名，{tld} 替换为顶级域（如 com）。
# 第一项是全局 RDAP 聚合（覆盖全部 TLD），后两项是常见 TLD 的官方快速端点，
# 用于 rdap.org 在个别 TLD（如 .top）响应慢/超时的回退。
_DEFAULT_SOURCES = [
    "https://rdap.org/domain/{domain}",
    "https://rdap.verisign.com/{tld}/v1/domain/{domain}",
]

# 常见公开后缀：用于剥出注册级域名（example.co.uk -> example.co.uk），
# 仅在未命中 RDAP 公共后缀表时兜底（见 _registrable()）。
_COMMON_SUFFIXES = {
    "com", "net", "org", "io", "co", "cn", "com.cn", "net.cn", "org.cn",
    "com.hk", "hk", "com.tw", "tw", "com.sg", "sg", "com.au", "au",
    "de", "fr", "jp", "co.jp", "in", "ru", "br", "mx", "it", "es",
    "pl", "uk", "co.uk", "org.uk", "me", "xyz", "top", "site", "app",
    "dev", "tech", "online", "store", "info", "biz", "moobi", "mobi",
}

# 单字母/两字母 ccTLD 不在列表中的自动剥离一级：a.b -- public_suffix=a,b
_fqdn_re = re.compile(r"^(?=.{1,253}$)([a-z0-9_][a-z0-9_-]*\.)+[a-z]{2,}$", re.IGNORECASE)

_lock = threading.Lock()
_domain_cache = {}      # registrable domain -> summary
_queries_done = 0
_config = {}


def _plugin_config(engine):
    base = {
        "enabled": True,
        "timeout": _TIMEOUT,
        "max_queries": 200,
        "cache_by_domain": True,
        "sources": list(_DEFAULT_SOURCES),
    }
    pc = getattr(engine, "config", {}).get("plugins") or {}
    if isinstance(pc, dict):
        override = pc.get("domain_whois") or {}
        if isinstance(override, dict):
            base.update(override)
    return base


def on_scan_started(engine):
    """Reset per-scan cache, budget and settings."""
    global _domain_cache, _queries_done, _config
    with _lock:
        _domain_cache = {}
        _queries_done = 0
    _config = _plugin_config(engine)
    logger.info("domain_whois active: max_queries=%s cache_by_domain=%s sources=%s",
                _config.get("max_queries"), _config.get("cache_by_domain"),
                _config.get("sources"))


def _registrable(fqdn):
    """剥出注册级域名（RDAP 只需查询 e.g. example.com，子域共享缓存）。

    返回注册级小写域名；无法识别的逐个返回。
    """
    fqdn = str(fqdn or "").strip().lower().rstrip(".")
    if not fqdn or not _fqdn_re.match(fqdn):
        return fqdn
    labels = fqdn.split(".")
    # 尝试 RDAP 查询该域名，若 404 再往上剥一层：简单启发式即可，
    # 因为注册级域名查询本身会返回数据、子域查询会 404。
    for i in range(0, len(labels) - 1):
        cand = ".".join(labels[i:])
        parts = cand.split(".")
        if len(parts) >= 2:
            suffix = ".".join(parts[1:])
            if suffix in _COMMON_SUFFIXES:
                return cand
    # 兜底：两级域名加一级公共后缀
    if len(labels) >= 3:
        return ".".join(labels[-2:]) if labels[-1] in _COMMON_SUFFIXES else ".".join(labels[-3:])
    return fqdn


def _vcard_entity(entity):
    """Extract a readable summary from an RDAP ``entities[]`` element."""
    roles = entity.get("roles", []) or []
    out = {"role": roles[0] if roles else ""}
    if entity.get("handle"):
        out["handle"] = entity["handle"]
    for line in entity.get("vcardArray", []) or []:
        if not isinstance(line, list):
            continue
        for v in line:
            if not isinstance(v, list) or len(v) < 3:
                continue
            key = str(v[0]).lower()
            val = v[3] if len(v) > 3 else v[-1]
            label = {
                "fn": "name", "org": "organization",
                "email": "email", "tel": "phone",
            }.get(key)
            if label and not out.get(label):
                if isinstance(val, list):
                    val = " ".join(str(x) for x in val if not isinstance(x, dict))
                out[label] = str(val)
    return out


def _fetch(domain, timeout, sources=None):
    if requests is None:
        raise RuntimeError("requests not installed")
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    sources = sources or _DEFAULT_SOURCES
    last_err = None
    for tpl in sources:
        url = tpl.format(domain=domain, tld=tld)
        try:
            resp = requests.get(url, timeout=timeout,
                                headers={"Accept": "application/rdap+json, application/json"})
            if resp.status_code in (200, 404, 410):
                # 404/410 = 无 RDAP 记录（域名不存在或 TLD 无 RDAP），直接给空数据
                if resp.status_code in (404, 410):
                    return {}
                return resp.json()
            last_err = RuntimeError(f"{url} returned HTTP {resp.status_code}")
        except Exception as e:
            last_err = e
            logger.debug("rdap (whois) source %s failed for %s: %s", url, domain, e)
    raise last_err or RuntimeError("no RDAP source configured")


def _find_event(events, action):
    for e in events or []:
        if str(e.get("eventAction") or "").lower() == action.lower():
            return e.get("eventDate")
    return None


def _summarize(data):
    """Condense a raw RDAP domain response into a compact dict."""
    if not data or not isinstance(data, dict):
        return {}
    entities = [_vcard_entity(e) for e in data.get("entities", []) if isinstance(e, dict)]
    registrar = next((e for e in entities if e.get("role") == "registrar"), None)
    registrant = next((e for e in entities if e.get("role") in ("registrant", None)), None)
    abuse = next((e for e in entities if e.get("role") == "abuse"), None)

    summary = {
        "handle": data.get("handle"),
        "status": data.get("status"),
    }
    if registrar and (registrar.get("name") or registrar.get("organization")):
        summary["registrar"] = registrar.get("organization") or registrar.get("name")
    if registrant and (registrant.get("name") or registrant.get("organization")):
        summary["registrant"] = registrant.get("organization") or registrant.get("name")
    if registrant and registrant.get("email"):
        summary["registrant_email"] = registrant["email"]
    if abuse and (abuse.get("email") or abuse.get("phone")):
        summary["abuse"] = {k: v for k, v in abuse.items() if k in ("email", "phone") and v}

    created = _find_event(data.get("events"), "registration")
    expires = _find_event(data.get("events"), "expiration")
    if created:
        summary["created"] = created
    if expires:
        summary["expires"] = expires

    nss = [n.get("ldhName") for n in (data.get("nameservers") or []) if isinstance(n, dict)]
    if nss:
        summary["nameservers"] = sorted(set(nss))

    ds = data.get("secureDNS") or {}
    if ds:
        summary["dnssec"] = dict(ds)
        summary["dnssec"].pop("dsData", None)

    return {k: v for k, v in summary.items() if v is not None}


def on_asset_scanned(asset, new_assets):
    global _queries_done
    if requests is None:
        return
    if not _config.get("enabled", True) or getattr(asset, "type", None) != "domain":
        return
    if "whois" in asset.properties:
        return
    fqdn = str(asset.value or "").strip().rstrip(".")
    if not fqdn or not _fqdn_re.match(fqdn):
        return

    target = _registrable(fqdn)
    if _config.get("cache_by_domain", True):
        with _lock:
            hit = _domain_cache.get(target)
        if hit is not None:
            asset.properties["whois"] = dict(hit)
            return

    with _lock:
        if _queries_done >= int(_config.get("max_queries", 200)):
            return
        _queries_done += 1

    timeout = float(_config.get("timeout", _TIMEOUT))
    sources = _config.get("sources") or _DEFAULT_SOURCES
    try:
        data = _fetch(target, timeout, sources=sources)
    except Exception as e:
        logger.warning("rdap (whois) query for %s failed on all sources: %s", target, e)
        summary = {"error": "query failed: %s" % (e,)}
        with _lock:
            _domain_cache[target] = summary
        asset.properties["whois"] = dict(summary)
        return

    summary = _summarize(data)
    if not summary:
        summary = {"error": "no RDAP record found"}
    with _lock:
        _domain_cache[target] = summary
    asset.properties["whois"] = summary


def on_scan_completed(engine):
    """Log a short summary and write a WHOIS enrichment report to the run dir."""
    with _lock:
        unique = len(_domain_cache)
        total = _queries_done
    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    logger.info("domain_whois: %s queries, %s unique domains cached", total, unique)
    if not outdir:
        return
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "whois_report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"queries": total, "domains": _domain_cache},
                      fh, ensure_ascii=False, indent=2)
        logger.info("whois report written: %s", path)
    except Exception as e:
        logger.warning("failed to write whois report: %s", e)


def plugin_help():
    return (
        "domain_whois - domain WHOIS enrichment via free global RDAP\n"
        "Adds asset.properties['whois'] with: registrar, registrant, status,\n"
        "created, expires, nameservers, dnssec, abuse contact.\n"
        "Bootstrap: https://rdap.org/domain/<domain> (all registries)\n"
        "Configure via plugins.domain_whois.{enabled,max_queries,timeout}"
    )