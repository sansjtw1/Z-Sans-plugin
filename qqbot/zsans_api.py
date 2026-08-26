"""zsans_api — 本地 Z-Sans Web 服务(zsansapi) 的极简 HTTP 客户端。

仅依赖标准库；机器人处理器通过 asyncio.to_thread 调用，避免阻塞事件循环。

覆盖机器人用到的接口:
    GET  /api/health            健康检查
    POST /api/scan/start        启动扫描  {seeds:[{type,value}], config:{}}
    POST /api/tasks/stop        停止任务  {id}
    GET  /api/tasks             任务列表
    GET  /api/tasks/<id>        任务详情(状态/指标/run_dir)
    GET  /api/tasks/<id>/logs   任务日志
    GET  /api/projects/<id>     项目资产图 JSON（用于报告统计）
"""

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT_DEFAULT = 30


class ZSansApiError(Exception):
    """zsansapi 调用失败。"""


class ZSansApi(object):
    def __init__(self, base_url, password="", timeout=_TIMEOUT_DEFAULT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json",
                        "Content-Type": "application/json"}
        if password:
            token = "Bearer {0}".format(password)
            self.headers["Authorization"] = token
            self.headers["X-API-Key"] = password

    # -- 底层请求 -----------------------------------------------------------

    def _request(self, method, path, body=None):
        url = self.base_url + path
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ZSansApiError("HTTP {0}: {1} {2}".format(e.code, e.reason, detail)) from e
        except urllib.error.URLError as e:
            raise ZSansApiError("连接失败({0}): {1}".format(url, e.reason)) from e
        except Exception as e:  # noqa: BLE001
            raise ZSansApiError("请求异常({0}): {1}".format(url, e)) from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ZSansApiError("响应不是 JSON: {0}".format(raw[:120])) from e

    # -- 接口封装 -----------------------------------------------------------

    def health(self):
        return self._request("GET", "/api/health")

    def start_scan(self, seeds, overrides=None):
        payload = {"seeds": seeds}
        if overrides:
            payload["config"] = overrides
        data = self._request("POST", "/api/scan/start", payload)
        tid = data.get("id")
        if not tid:
            raise ZSansApiError("启动扫描未返回任务 ID: {0}".format(data))
        return str(tid)

    def stop_task(self, task_id):
        data = self._request("POST", "/api/tasks/stop", {"id": str(task_id)})
        return bool(data.get("ok"))

    def get_task(self, task_id):
        return self._request("GET", "/api/tasks/{0}".format(urllib.parse.quote(str(task_id))))

    def list_tasks(self):
        data = self._request("GET", "/api/tasks")
        return data if isinstance(data, list) else []

    def get_logs_tail(self, task_id, tail=20):
        data = self._request(
            "GET", "/api/tasks/{0}/logs?tail={1}".format(urllib.parse.quote(str(task_id)), int(tail)))
        return data.get("logs") or []

    def get_project(self, project_id):
        """项目不存在/进行中时返回 None（不抛异常）。"""
        if not re.match(r"^\d{8}_\d{6}$", str(project_id or "")):
            return None
        try:
            return self._request("GET", "/api/projects/{0}".format(project_id))
        except ZSansApiError:
            return None


# ---------------------------------------------------------------------------
# 种子解析（与 webapp.validate_seed 同源的宽松白名单版，供 QQ 指令使用）
# ---------------------------------------------------------------------------

_HOST_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _valid_hostname(host):
    if not host or len(host) > 253:
        return False
    labels = host.lower().rstrip(".").split(".")
    return all(_HOST_LABEL_RE.match(lb) for lb in labels)


def classify_target(token):
    """把一个用户输入分类为 seed dict 或 (None, 错误原因)。"""
    value = token.strip()
    if not value or len(value) > 2048:
        return None, "空值或超长"

    lowered = value.lower()

    # URL：带协议头 或 含端口/路径特征
    if lowered.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(lowered)
        host = (parsed.hostname or "").strip("[]")
        if parsed.scheme not in ("http", "https") or not host:
            return None, value
        if not (_is_ip_literal(host) or _valid_hostname(host)):
            return None, value
        return {"type": "url", "value": lowered}, None

    # IP 字面量（v4/v6）
    if _is_ip_literal(value):
        return {"type": "ip", "value": value.strip("[]")}, None

    # 域名
    if _valid_hostname(lowered):
        return {"type": "domain", "value": lowered.rstrip(".")}, None

    return None, value


def _is_ip_literal(value):
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False
