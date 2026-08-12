"""
IP 归属地中文查询插件(Z-Sans)
================================
通过 ip9.com.cn 免费接口查询每个 IP 资产的归属地信息,以中文字段
(国家/省份/城市/运营商/经纬度等)写入资产 ``properties["ip9"]``,
随 JSON / CSV / GraphML 导出。扫描结束时额外输出一份中文汇总报告
到本次运行的时间戳子目录。

Manifest:
    name: ip_info_cn
    version: 1.0.0
    description: 通过 ip9.com.cn 查询 IP 归属地信息(中文输出)
    author: Z-Sans SansJtw

支持的资产类型:
    - ip  (IPv4 / IPv6)

配置(breeding-config.yaml):
    plugins:
      ip_info_cn:
        enabled: true          # 总开关(默认 true)
        timeout: 10            # 单次请求超时秒数(默认 10)
        skip_private: true     # 跳过内网 / 保留地址(默认 true)
        report: true           # 扫描结束时是否写中文汇总报告(默认 true)

环境变量:
    IP_INFO_CN_TIMEOUT          # 覆盖 timeout
    IP_INFO_CN_SKIP_PRIVATE     # 覆盖 skip_private
"""

import ipaddress
import logging
import os
import threading

import requests

logger = logging.getLogger("zsans.plugin.ip_info_cn")

__manifest__ = {
    "name": "ip_info_cn",
    "version": "1.0.0",
    "description": "通过 ip9.com.cn 查询 IP 归属地信息(中文输出)",
    "author": "Z-Sans SansJtw",
}

# 接口地址:https://ip9.com.cn/get?ip=<ip>
_API_URL = "https://ip9.com.cn/get"

# ip9.com.cn 返回字段 → 中文标签
_FIELD_LABELS = {
    "country": "国家",
    "country_code": "国家代码",
    "prov": "省份",
    "city": "城市",
    "area": "地区",
    "post_code": "邮编",
    "isp": "运营商",
    "lng": "经度",
    "lat": "纬度",
}

# 模块级状态(每次扫描 on_scan_started 时重置)
_engine = None
_config = {}
_stats = {
    "queried": 0,
    "success": 0,
    "failed": 0,
    "skipped_private": 0,
}
_ip9_results = {}   # ip -> 中文归属地 dict
_state_lock = threading.Lock()


def _resolve_config(engine):
    """从 breeding-config.yaml 与环境变量读取配置。"""
    global _config
    plugin_cfg = {}
    if engine and hasattr(engine, "config"):
        pcfg = engine.config.get("plugins", {})
        if isinstance(pcfg, dict):
            plugin_cfg = pcfg.get("ip_info_cn", {}) or {}
    _config = {
        "enabled": plugin_cfg.get("enabled", True),
        "timeout": float(os.environ.get("IP_INFO_CN_TIMEOUT", plugin_cfg.get("timeout", 10))),
        "skip_private": _as_bool(
            os.environ.get("IP_INFO_CN_SKIP_PRIVATE", plugin_cfg.get("skip_private", True))
        ),
        "report": plugin_cfg.get("report", True),
    }
    logger.debug("ip_info_cn config: %s", _config)


def _as_bool(v):
    """宽松地把字符串 / bool / int 转成 bool。"""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _is_private_ip(ip):
    """内网 / 回环 / 链路本地等无需外查的地址返回 True。"""
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


def _query(ip):
    """调用 ip9.com.cn 查询单个 IP,返回中文字段 dict;失败返回 None。"""
    try:
        r = requests.get(_API_URL, params={"ip": ip}, timeout=_config.get("timeout", 10))
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or {}
    except Exception as e:
        logger.debug("ip_info_cn 查询 %s 失败: %s", ip, e)
        return None

    if payload.get("ret") != 200 or not data:
        logger.debug("ip_info_cn 查询 %s 无数据: %s", ip, payload)
        return None

    # 转成中文字段,空值丢弃
    result = {"IP地址": data.get("ip") or ip}
    for key, label in _FIELD_LABELS.items():
        val = data.get(key)
        if val not in (None, ""):
            result[label] = val
    return result


def _process_ip_asset(asset):
    """给一个 IP 资产标注中文归属地信息。"""
    global _stats, _ip9_results
    if not _config.get("enabled", True):
        return
    if asset is None or getattr(asset, "type", "") != "ip":
        return
    if "ip9" in getattr(asset, "properties", {}):
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

    info = _query(ip)
    if info:
        try:
            asset.properties["ip9"] = info
        except Exception as e:
            logger.debug("ip_info_cn 无法标注 %s: %s", ip, e)
        with _state_lock:
            _stats["success"] += 1
            _ip9_results[ip] = info
        logger.info("ip_info_cn: %s -> %s %s %s", ip, info.get("国家", ""), info.get("省份", ""), info.get("运营商", ""))
    else:
        with _state_lock:
            _stats["failed"] += 1


# ─────────────────────────────────────────────
# 事件处理器(引擎按 on_ 前缀自动注册)
# ─────────────────────────────────────────────

def on_scan_started(engine):
    """读取配置并重置本次扫描的统计状态。"""
    global _engine, _stats, _ip9_results
    _engine = engine
    _resolve_config(engine)
    with _state_lock:
        _stats = {"queried": 0, "success": 0, "failed": 0, "skipped_private": 0}
        _ip9_results = {}


def on_asset_discovered(asset, source):
    """每发现一个 IP 资产即查询其中文归属地。"""
    _process_ip_asset(asset)


def on_scan_completed(engine):
    """把中文归属地汇总报告写到本次运行的时间戳子目录。"""
    if not _config.get("report", True) or not _engine:
        return
    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not outdir:
        return
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "ip9_info_report.json")
        with _state_lock:
            stats = dict(_stats)
            results = dict(_ip9_results)
        import json as _json
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({
                "插件": "ip_info_cn",
                "版本": __manifest__["version"],
                "接口": _API_URL,
                "统计": stats,
                "结果": results,
            }, f, ensure_ascii=False, indent=2)
        logger.info("ip_info_cn 中文归属地报告: %s", path)
    except Exception as e:
        logger.warning("ip_info_cn 报告写入失败: %s", e)
