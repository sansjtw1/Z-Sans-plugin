"""qqbot — 通过 QQ 官方机器人（nonebot-adapter-qq）远程操控 Z-Sans。

在 QQ 中发送指令即可 启动扫描 / 终止扫描 / 查询进度 / 获取报告：

    扫描 example.com --depth 3
    状态
    停止 task-1
    报告 task-1

运行方式（本插件提供的独立 CLI 入口，参数均带 qqbot 前缀避免冲突）:

    python main.py --qqbot                 # 自动拉起 Web 服务(zsansapi) + QQ 机器人
    python main.py --qqbot --port 9000     # 指定 Web 控制台端口
    python main.py --qqbot --qqbot-port 8078  # 指定 NoneBot 监听端口

配置来源（低 -> 高优先级覆盖）:
    1. 本目录 config.yaml 出厂默认
    2. breeding-config.yaml 的 plugins.qqbot 段
    3. Web 控制台 qqbot 插件页保存的 output/plugin_config/qqbot.yaml

本插件不注册任何 on_* 扫描钩子：机器人通过本地 zsansapi(HTTP) 下发与跟踪
扫描任务，与引擎完全解耦，扫描进程崩溃也不影响机器人应答。
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # 主程序已依赖 PyYAML，此处仅防御
    yaml = None

logger = logging.getLogger("zsans.plugin.qqbot")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class _BotProcess:
    """管理 QQ 机器人子进程（热启停）。"""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            if self._proc is None:
                return False
            if self._proc.poll() is None:
                return True
            self._proc = None
            return False

    @property
    def pid(self):
        with self._lock:
            return self._proc.pid if self._proc and self._proc.poll() is None else None

    def start(self, config_path=None):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return False, "机器人已在运行中 (PID {0})".format(self._proc.pid)
            python = sys.executable
            cmd = [python, os.path.join(_ensure_project_root() or ".", "main.py"),
                   "--qqbot-bot-only"]
            if config_path:
                cmd.extend(["-c", config_path])
            env = os.environ.copy()
            env["ZSANS_QQBOT_BOT_ONLY"] = "1"
            try:
                self._proc = subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True, "机器人已启动 (PID {0})".format(self._proc.pid)
            except Exception as e:
                return False, "启动失败: {0}".format(e)

    def stop(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                return True, "机器人未在运行"
            pid = self._proc.pid
            try:
                os.kill(pid, signal.SIGTERM)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._proc = None
            return True, "机器人已停止 (PID {0})".format(pid)


_bot_process = _BotProcess()

__manifest__ = {
    "name": "qqbot",
    "version": "1.0.0",
    "description": "QQ official bot via nonebot-adapter-qq: start/stop scans and "
                   "receive reports with QQ commands; auto-starts the web API (zsansapi). "
                   "Provides main.py --qqbot",
    "author": "Z-Sans Contributors",
    "webui": "webui.html",
}


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_yaml_file(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) if yaml else json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("read yaml %s failed: %s", path, e)
        return {}


def _breed_plugin_section(config_path):
    """读取 breeding-config.yaml 中 plugins.qqbot 段。"""
    data = _load_yaml_file(config_path)
    plugins = data.get("plugins")
    if isinstance(plugins, dict) and isinstance(plugins.get("qqbot"), dict):
        return plugins["qqbot"]
    return {}


def _web_saved_config(config_path=None, output_dir=None):
    """读取 Web 控制台为插件保存的配置（output/plugin_config/qqbot.yaml）。"""
    candidates = []
    if output_dir:
        candidates.append(os.path.join(output_dir, "plugin_config", "qqbot.yaml"))
    # 未显式给出输出目录时，按常见位置探测（cwd 相对 output/）
    candidates.append(os.path.join(os.getcwd(), "output", "plugin_config", "qqbot.yaml"))
    for p in candidates:
        data = _load_yaml_file(p)
        if data:
            return data
    return {}


def _deep_merge(base, override):
    merged = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def effective_config(config_path=None, output_dir=None):
    """合并三层配置源，返回最终生效的插件配置。"""
    bundled = _load_yaml_file(os.path.join(PLUGIN_DIR, "config.yaml"))
    breed = _breed_plugin_section(config_path)
    saved = _web_saved_config(config_path, output_dir)
    cfg = _deep_merge(_deep_merge(bundled, breed), saved)
    if not isinstance(cfg.get("intents"), dict):
        cfg["intents"] = {}
    if not isinstance(cfg.get("commands"), dict):
        cfg["commands"] = {}
    if not isinstance(cfg.get("messages"), dict):
        cfg["messages"] = {}
    return cfg


# ---------------------------------------------------------------------------
# CLI（--qqbot / --qqbot-port，命名带前缀，绝不与核心或其他插件参数冲突）
# ---------------------------------------------------------------------------

def register_cli(parser):
    parser.add_argument(
        "--qqbot", action="store_true",
        help="Start the QQ bot gateway provided by the qqbot plugin "
             "(auto-starts the web console/zsansapi first)",
    )
    parser.add_argument(
        "--qqbot-port", type=int, default=None, metavar="PORT",
        help="Override the local NoneBot listen port (qqbot plugin; "
             "default: plugins.qqbot.bot_port)",
    )
    parser.add_argument(
        "--qqbot-bot-only", action="store_true", default=False,
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(_zplugin_cli=run_cli, _zplugin_active="qqbot")


# ---------------------------------------------------------------------------
# 工程根定位（用于 import main/webapp）
# ---------------------------------------------------------------------------

def _find_project_root():
    """向上查找包含 main.py + webapp.py 的目录。"""
    seeds = [os.getcwd(), PLUGIN_DIR]
    for seed in seeds:
        cur = os.path.abspath(seed)
        for _ in range(6):
            if os.path.isfile(os.path.join(cur, "main.py")) and \
               os.path.isfile(os.path.join(cur, "webapp.py")):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return None


def _ensure_project_root():
    root = _find_project_root()
    if root is None:
        logger.warning(
            "[qqbot] 未找到 Z-Sans 工程根(main.py+webapp.py)，"
            "请确保在 Z-Sans 目录下运行 --qqbot"
        )
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    return root


# ---------------------------------------------------------------------------
# Web 服务(zsansapi) 自动启动
# ---------------------------------------------------------------------------

class _WebServerState(object):
    """记录后台 Web 线程的启动结果，便于失败时给出可读错误。"""
    def __init__(self):
        self.ready = threading.Event()
        self.error = None


def _run_web_server(base_config, config_path, output_dir, host, port, state):
    try:
        from webapp import start_web_server
        start_web_server(base_config, config_path, output_dir, port=port, host=host)
        state.error = RuntimeError("web server exited unexpectedly")
    except Exception as e:  # noqa: BLE001 - 线程内兜底，必须吞掉异常
        state.error = e
        logger.error("[qqbot] Web 服务启动失败: %s", e)
    finally:
        state.ready.set()


def _api_headers(password):
    headers = {"Accept": "application/json"}
    if password:
        headers["Authorization"] = "Bearer {0}".format(password)
        headers["X-API-Key"] = password
    return headers


def _wait_web_ready(host, port, password, timeout=25.0, state=None):
    """轮询 /api/health 直到 Web 服务可用或超时。"""
    url = "http://{0}:{1}/api/health".format(host, int(port))
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers=_api_headers(password))
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            last_err = "HTTP {0}".format(e.code)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if state is not None and state.error is not None:
            return False
        time.sleep(0.5)
    if state is not None and state.error is not None:
        return False
    logger.error("[qqbot] 等待 Web 服务就绪超时(%ss): %s (%s)", timeout, url, last_err)
    return False


def _start_web_service(cfg, args, state):
    """在守护线程中拉起 zsansapi。返回 (host, port)。"""
    from main import load_config  # 需要工程根已入 sys.path

    config_path = getattr(args, "config", None) or "breeding-config.yaml"

    # 端口决策：命令行显式值 > 插件配置 > 核心默认。
    # 核心 --port/--host 默认值分别为 8050/127.0.0.1；等于默认值时视为未显式指定，
    # 从而让插件配置里的 web_port/web_host 生效。
    web_port = cfg.get("web_port") or 8050
    if getattr(args, "port", None) not in (None, 8050):
        web_port = args.port
    web_host = cfg.get("web_host") or "127.0.0.1"
    if getattr(args, "host", None) not in (None, "127.0.0.1"):
        web_host = args.host

    # 认证口令需在 Web 服务创建前写入环境变量
    password = str(cfg.get("web_password") or "").strip()
    if password:
        os.environ.setdefault("ZSANS_WEB_PASSWORD", password)

    base_config = load_config(config_path) or {}
    out_cfg = base_config.get("output")
    output_dir = out_cfg.get("dir", "output") if isinstance(out_cfg, dict) else "output"

    t = threading.Thread(
        target=_run_web_server,
        args=(base_config, config_path, output_dir, web_host, int(web_port), state),
        name="zsans-web-api",
        daemon=True,
    )
    t.start()
    return web_host, int(web_port)


# ---------------------------------------------------------------------------
# QQ 机器人(nonebot) 启动
# ---------------------------------------------------------------------------

_INTENT_KEYS_WHITELIST = (
    "guilds", "guild_members", "guild_messages", "guild_message_reactions",
    "direct_message", "open_forum_event", "audio_live_member",
    "c2c_group_at_messages", "interaction", "message_audit", "forum_event",
    "audio_action", "at_messages",
)


def _build_qq_bots_env(cfg):
    """构造适配器所需的环境变量（QQ_BOTS/QQ_IS_SANDBOX/QQ_AUTH_BASE）。

    鉴权只需 AppID + AppSecret(clientSecret)：适配器会自动调用官方
    getAppAccessToken 接口换取 access_token（有效期 7200 秒）并在过期前
    自动续期，因此 ``token`` 为选填项。

    返回 (bots_json_masked, error)；凭据缺失时 error 给出人话提示。
    """
    app_id = str(cfg.get("app_id") or "").strip()
    secret = str(cfg.get("app_secret") or "").strip()
    missing = [n for n, v in (("app_id", app_id), ("app_secret", secret)) if not v]
    if missing:
        return None, (
            "QQ 机器人凭据缺失: {0}。鉴权只需要 AppID 与 AppSecret(clientSecret)，"
            "请在 Web 控制台「插件 -> qqbot -> 进入」填写，或在 breeding-config.yaml "
            "的 plugins.qqbot 段配置后重启。".format(", ".join(missing))
        )

    # token 不参与鉴权(getAppAccessToken 流程)，仅为满足适配器模型必填键，
    # 未填写时以占位符代替。
    token = str(cfg.get("token") or "").strip() or "not-used"

    intents = {k: bool(v) for k, v in (cfg.get("intents") or {}).items()
               if k in _INTENT_KEYS_WHITELIST}
    bots = [{
        "id": app_id,          # adapter-qq >= 1.x 使用 id
        "appid": app_id,       # 兼容旧版本字段名（多余键会被忽略）
        "name": "zsans",
        "token": token,
        "secret": secret,
        "intent": intents,
        "use_websocket": bool(cfg.get("use_websocket", True)),
    }]
    os.environ["QQ_BOTS"] = json.dumps(bots, ensure_ascii=False)
    os.environ["QQ_IS_SANDBOX"] = "true" if cfg.get("sandbox") else "false"

    # 官方文档新鉴权端点；不配置时适配器回落到其内置默认(bots.qq.com)
    auth_base = str(cfg.get("auth_base") or "").strip()
    if auth_base:
        os.environ["QQ_AUTH_BASE"] = auth_base

    masked = json.dumps([{**b, "secret": "***", "token": "***"} for b in bots],
                        ensure_ascii=False)
    return masked, None


_DEFAULT_AUTH_BASE = "https://api.bot.qq.com/app/getAppAccessToken"

# getAppAccessToken 常见错误码提示（QQ 开放平台）
_AUTH_CODE_HINTS = {
    10004: "机器人不存在 —— 请检查 app_id(AppID) 是否填写正确",
    10003: "参数缺失或格式错误 —— 请检查 app_id/clientSecret 是否完整复制",
}


def _preflight_auth(cfg, retries=3):
    """启动前鉴权自检：直接调用官方 getAppAccessToken 换取 access_token。

    对应官方文档「调用官方API需要 access_token」一节：
    POST {appId, clientSecret} -> {"access_token": "...", "expires_in": "7200"}

    鉴权只需 AppID + AppSecret；token 由本接口签发，机器人运行期间适配器
    会在过期前自动续期。网络抖动(如 DNS 偶发失败)时自动重试。

    返回 (ok, 提示消息)。失败时消息包含可操作的排查建议。
    """
    app_id = str(cfg.get("app_id") or "").strip()
    secret = str(cfg.get("app_secret") or "").strip()
    auth_base = str(cfg.get("auth_base") or "").strip() or _DEFAULT_AUTH_BASE

    body = json.dumps({"appId": app_id, "clientSecret": secret}).encode("utf-8")
    last_err = None
    for i in range(1, max(1, retries) + 1):
        req = urllib.request.Request(
            auth_base, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001 - 网络类错误重试
            last_err = e
            if i < retries:
                wait = i * 3
                logger.warning(
                    "[qqbot] 鉴权接口连接失败({0}: {1})，{2}s 后重试 "
                    "{3}/{4}…".format(type(e).__name__, e, wait, i, retries))
                time.sleep(wait)
    else:
        return False, (
            "无法连接鉴权接口 {0}: {1}\n"
            "可能原因: 网络不通/DNS 解析异常/被代理拦截。请检查服务器网络后重启；"
            "若 AppID 正确却持续被拒，请核对 clientSecret(AppSecret)。"
            .format(auth_base, last_err)
        )

    try:
        data = json.loads(raw)
    except ValueError:
        return False, "鉴权接口返回非 JSON: {0}".format(raw[:200])

    if not isinstance(data, dict) or "access_token" not in data:
        code = data.get("code")
        message = data.get("message") or raw[:200]
        hint = _AUTH_CODE_HINTS.get(code if isinstance(code, int) else 0,
                                    "请到开放平台「开发设置」核对凭据")
        return False, "鉴权失败: code={0} message={1}\n提示: {2}".format(
            code, message, hint)

    expires = data.get("expires_in")
    token = str(data.get("access_token"))
    masked = token[:6] + "***" + token[-4:] if len(token) > 12 else "***"
    return True, (
        "鉴权自检通过 ✓ 已获取 access_token={0}（有效期 {1} 秒，"
        "机器人运行期间会自动续期）".format(masked, expires)
    )


def _wait_dns(host, timeout=60.0):
    """等待域名可解析（部分环境 DNS 偶发失败/首查极慢）。

    成功返回 True；超时返回 False 并记录排查建议。
    """
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            import socket
            socket.getaddrinfo(host, 443)
            if attempt > 1:
                logger.info("[qqbot] %s 解析恢复", host)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[qqbot] DNS 解析 {0} 失败({1})，第 {2} 次重试…".format(
                host, e, attempt))
            time.sleep(3)
    logger.error(
        "[qqbot] 域名 {host} 在 {timeout}s 内始终无法解析。\n"
        "请检查服务器 DNS(如 /etc/resolv.conf) 或网络出口；"
        "也可在 hosts 中为该域名固定 IP 后重启。".format(host=host, timeout=int(timeout)))
    return False


def _hold_process():
    """保持进程存活（Web 服务在守护线程中运行）。Ctrl+C 退出。"""
    print("")
    print("=" * 62)
    print("  进程保持运行中 — Web 控制台可正常访问")
    print("  按 Ctrl+C 退出")
    print("=" * 62)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("[qqbot] 用户中断，进程退出。")


def _run_bot_standalone(config_path=None):
    """独立运行 QQ 机器人（不含 Web 服务），供热启停子进程调用。"""
    root = _ensure_project_root()
    cp = config_path or os.path.join(root or ".", "breeding-config.yaml")
    cfg = effective_config(config_path=cp)
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        logger.error("[qqbot-bot] 未配置 AppID/AppSecret，无法启动机器人。")
        return 2
    if cfg.get("auth_check", True):
        ok, msg = _preflight_auth(cfg)
        (logger.info if ok else logger.error)("[qqbot-bot] %s", msg)
        if not ok:
            return 2
    gateway_host = ("sandbox.api.sgroup.qq.com" if cfg.get("sandbox")
                    else "api.sgroup.qq.com")
    auth_host = urllib.parse.urlparse(
        str(cfg.get("auth_base") or "").strip() or _DEFAULT_AUTH_BASE).hostname
    for host in dict.fromkeys([h for h in (gateway_host, auth_host) if h]):
        if not _wait_dns(host, timeout=45):
            return 4

    class _Args:
        pass
    args = _Args()
    return _run_bot(cfg, args)


def _run_bot(cfg, args):
    """初始化 nonebot 并阻塞运行 QQ 机器人。返回进程退出码。"""
    try:
        import nonebot
    except ImportError:
        logger.error(
            "[qqbot] 未安装 nonebot2。请先执行:\n"
            "    pip install -r \"{0}\"\n"
            "或: pip install \"nonebot2[httpx,websockets]\" nonebot-adapter-qq"
            .format(os.path.join(PLUGIN_DIR, "requirements.txt"))
        )
        return 2
    try:
        from nonebot.adapters.qq import Adapter as QQAdapter
    except ImportError:
        logger.error(
            "[qqbot] 未安装 nonebot-adapter-qq。请执行:\n"
            "    pip install -r \"{0}\"".format(os.path.join(PLUGIN_DIR, "requirements.txt"))
        )
        return 2

    bots_json, err = _build_qq_bots_env(cfg)
    if err:
        logger.error("[qqbot] %s", err)
        return 2

    use_ws = bool(cfg.get("use_websocket", True))
    driver = "~httpx+~websockets" if use_ws else "~fastapi"

    bot_port = cfg.get("bot_port") or 8078
    if getattr(args, "qqbot_port", None):
        bot_port = args.qqbot_port
    bot_host = cfg.get("bot_host") or "127.0.0.1"

    nonebot.init(driver=driver, host=bot_host, port=int(bot_port))
    qdriver = nonebot.get_driver()
    qdriver.register_adapter(QQAdapter)

    # 注入配置与 API 客户端后再加载指令处理器模块
    from zsans_api import ZSansApi
    import zsans_qq_bot

    api = ZSansApi(
        "http://{0}:{1}".format(cfg.get("web_host") or "127.0.0.1",
                                cfg.get("web_port") or 8050),
        password=str(cfg.get("web_password") or ""),
    )
    zsans_qq_bot.configure(cfg, api)

    mode = "WebSocket 反向连接" if use_ws else "WebHook 回调"
    sandbox = "沙箱" if cfg.get("sandbox") else "正式"
    print("")
    print("=" * 62)
    print("  Z-Sans QQ Bot 已启动 (nonebot-adapter-qq)")
    print("    运行模式 : {0} ({1})".format(mode, sandbox))
    print("    监听地址 : http://{0}:{1}".format(bot_host, bot_port))
    print("    本地 API : {0}  (zsansapi 已随行启动)".format(api.base_url))
    triggers = cfg.get("commands") or {}
    scan_words = ", ".join(_as_list(triggers.get("scan")))
    help_words = ", ".join(_as_list(triggers.get("help")))
    print("    试一试   : 在 QQ 里 @机器人 发送「{0} example.com」".format(
        scan_words.split(",")[0].strip() if scan_words else "scan"))
    print("    全部指令 : 发送「{0}」查看".format(
        help_words.split(",")[0].strip() if help_words else "help"))
    print("=" * 62)
    logger.info("[qqbot] QQ_BOTS=%s", bots_json)

    # 网络抖动(尤其 DNS 偶发解析失败)会让首次网关连接直接失败退出，
    # 这里对「启动后短时间内即异常结束」的情况做有限次自动重启。
    attempts = 1
    try:
        attempts = max(1, min(10, int(cfg.get("startup_retries", 3))))
    except (TypeError, ValueError):
        attempts = 3

    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            nonebot.run()
            return 0                     # 正常退出(Ctrl+C / 主动停止)
        except KeyboardInterrupt:
            return 0
        except OSError as e:
            logger.error("[qqbot] NoneBot 监听失败(端口被占用?): %s", e)
            return 3
        except Exception as e:  # noqa: BLE001 - 网络/适配器层瞬时错误统一重试
            elapsed = time.time() - started
            if attempt >= attempts or elapsed > 60:
                logger.error("[qqbot] 启动失败(第 {0}/{1} 次尝试): {2}: {3}".format(
                    attempt, attempts, type(e).__name__, e))
                logger.error("[qqbot] 若持续失败，请检查网络/DNS 后手动重启，"
                             "或调大 plugins.qqbot.startup_retries")
                return 4
            wait = min(30, attempt * 5)
            logger.warning(
                "[qqbot] 第 {0}/{1} 次启动在 {2:.0f}s 内异常结束({3}: {4})，"
                "{5}s 后自动重试…".format(attempt, attempts, elapsed,
                                          type(e).__name__, e, wait))
            time.sleep(wait)
    return 4


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def run_cli(args):
    """python main.py --qqbot 的主入口。

    流程: 合并配置 -> 守护线程拉起 Web 服务(zsansapi) -> 等待就绪 -> 启动 QQ 机器人。
    若未配置 app_id/app_secret，则仅启动 Web 服务供用户在页面上配置，
    进程保持存活，配置完成后可通过 WebUI「启动机器人」按钮热启。
    """
    logger.info("[qqbot] 正在准备 QQ 机器人网关…")

    root = _ensure_project_root()

    config_path = getattr(args, "config", None) or os.path.join(root or ".", "breeding-config.yaml")
    cfg = effective_config(config_path=config_path)
    if cfg.get("enabled") is False:
        logger.error("[qqbot] 插件已被禁用(breeding-config.yaml -> plugins.qqbot.enabled=false)")
        return 2

    # 1) 自动启动 Web 服务（zsansapi）
    state = _WebServerState()
    web_host, web_port = _start_web_service(cfg, args, state)
    logger.info("[qqbot] 正在自动启动 Web 服务(zsansapi): http://%s:%d …", web_host, web_port)
    if not _wait_web_ready(web_host, web_port, str(cfg.get("web_password") or ""), state=state):
        if state.error is not None:
            logger.error("[qqbot] Web 服务未能启动: %s", state.error)
        return 1
    logger.info("[qqbot] Web 服务(zsansapi) 就绪: http://%s:%d", web_host, web_port)

    # 2) 检查凭据是否已配置
    has_creds = bool(str(cfg.get("app_id") or "").strip()
                     and str(cfg.get("app_secret") or "").strip())
    if not has_creds:
        logger.warning(
            "[qqbot] ⚠ 尚未配置 AppID/AppSecret，机器人不会自动启动。\n"
            "    请打开 Web 控制台 -> qqbot 插件页填写凭据后，\n"
            "    点击「启动机器人」按钮即可热启，无需重启进程。\n"
            "    当前 Web 服务地址: http://%s:%d", web_host, web_port)
        _hold_process()
        return 0

    # 3) 鉴权自检：按官方 getAppAccessToken 流程预先换取 access_token，
    #    在启动机器人前把 AppID/AppSecret 问题一次性暴露清楚
    if cfg.get("auth_check", True):
        ok, msg = _preflight_auth(cfg)
        (logger.info if ok else logger.error)("[qqbot] %s", msg)
        if not ok:
            logger.warning("[qqbot] 鉴权失败，机器人未启动。请修正凭据后通过 WebUI 热启。")
            _hold_process()
            return 2

    # 4) 网关域名预解析：把 DNS 抖动挡在启动之前
    gateway_host = ("sandbox.api.sgroup.qq.com" if cfg.get("sandbox")
                    else "api.sgroup.qq.com")
    auth_host = urllib.parse.urlparse(
        str(cfg.get("auth_base") or "").strip() or _DEFAULT_AUTH_BASE).hostname
    for host in dict.fromkeys([h for h in (gateway_host, auth_host) if h]):
        if not _wait_dns(host, timeout=45):
            logger.warning("[qqbot] DNS 预解析失败，机器人未启动。请检查网络后通过 WebUI 热启。")
            _hold_process()
            return 4

    # 5) 启动 QQ 机器人（阻塞直至 Ctrl+C）
    return _run_bot(cfg, args)
