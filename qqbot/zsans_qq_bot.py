"""zsans_qq_bot — Z-Sans 的 QQ 指令处理器（nonebot 插件模块）。

仅由 qqbot/plugin.py 在 ``--qqbot`` 模式下导入运行；普通扫描流程不会加载本模块，
因此主程序不依赖 nonebot。

指令（触发词可在配置中自定义）::

    扫描 example.com --depth 3     启动扫描（可多个目标，域名/URL/IP 混排）
    停止 [任务ID]                  终止扫描（默认最近一次自己发起的）
    状态 [任务ID]                  查询进度
    任务                           列出最近任务
    报告 <任务ID>                  获取报告摘要
    帮助                           显示帮助

任务完成后自动把报告推送到发起会话。受 QQ 官方平台限制，被动回复存在时效
（约 5 分钟），超时的推送会失败——此时用「报告 <任务ID>」随时补取。
"""

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict

from nonebot import on_message
from nonebot.adapters.qq import Bot as QBot
from nonebot.adapters.qq import MessageEvent as QQMessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from zsans_api import classify_target, ZSansApiError  # noqa: F401 (ZSansApi 经 plugin 注入)

logger = logging.getLogger("zsans.plugin.qqbot.bot")

# ---------------------------------------------------------------------------
# 运行时配置（由 plugin._run_bot 调用 configure() 注入）
# ---------------------------------------------------------------------------

CFG = {}
API = None  # type: "zsans_api.ZSansApi | None"

_STATUS_ICON = {
    "running": "⏳",
    "completed": "✅",
    "failed": "❌",
    "stopped": "⏹",
}


def configure(cfg, api):
    global CFG, API
    CFG = cfg or {}
    API = api


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


class _SafeDict(dict):
    """format_map 遇到未知占位符时保留原文而非抛错。"""

    def __missing__(self, key):
        return "{" + key + "}"


def _tmpl(key, **kwargs):
    """按模板渲染回复文本；已知占位符填充、未知占位符原样保留。

    模板缺失时返回空串；模板本身格式非法（如出现 {a[b]}）时回退原文。
    """
    templates = CFG.get("messages") or {}
    tpl = templates.get(key)
    if tpl is None:
        return ""
    try:
        return str(tpl).format_map(_SafeDict(**kwargs))
    except Exception:  # ValueError/IndexError —— 占位符语法被改坏时回退原文
        logger.warning("[qqbot] 消息模板 %s 渲染失败，回退为原文", key)
        return str(tpl)


def _commands():
    cmds = CFG.get("commands") or {}
    out = OrderedDict()
    for name, default in (
        ("help", ["帮助", "help"]),
        ("scan", ["扫描", "scan"]),
        ("stop", ["停止", "stop"]),
        ("status", ["状态", "status"]),
        ("tasks", ["任务", "tasks"]),
        ("report", ["报告", "report"]),
    ):
        out[name] = _as_list(cmds.get(name)) or list(default)
    return out


def _primary(name):
    aliases = _commands().get(name) or [name]
    return aliases[0]


# ---------------------------------------------------------------------------
# 会话任务登记表（进程内）
# ---------------------------------------------------------------------------

_SESSION_TASKS = {}   # session_key -> [ {tid,user,ts}, ... ]（新任务插头部）
_REG_LOCK = threading.Lock()
_MAX_TRACKED_PER_SESSION = 50


def _register_task(session_key, tid, user_id):
    with _REG_LOCK:
        rows = _SESSION_TASKS.setdefault(session_key, [])
        rows.insert(0, {"tid": str(tid), "user": str(user_id), "ts": time.time()})
        del rows[_MAX_TRACKED_PER_SESSION:]


def _lookup_task(session_key, tid=None, user_id=None, is_admin=False):
    """解析目标任务 ID：显式 ID > 本人最新 > 管理员兜底。

    返回 (tid, 错误码)；错误码取值: not_found / empty。
    """
    with _REG_LOCK:
        rows = list(_SESSION_TASKS.get(session_key) or [])
    if tid:
        if any(r["tid"] == str(tid) for r in rows):
            return str(tid), None
        # 不在本会话登记里：管理员可直接指定任意 ID；普通用户提示不存在
        return (str(tid), None) if is_admin else (None, "not_found")
    mine = [r for r in rows if r["user"] == str(user_id)]
    if mine:
        return mine[0]["tid"], None
    if rows and is_admin:
        return rows[0]["tid"], None
    return None, "empty"


def _unregister_task(session_key, tid):
    with _REG_LOCK:
        rows = _SESSION_TASKS.get(session_key) or []
        _SESSION_TASKS[session_key] = [r for r in rows if r["tid"] != str(tid)]


# ---------------------------------------------------------------------------
# 事件工具：文本提取 / 权限
# ---------------------------------------------------------------------------

_PREFIX_CHARS = "/！!？?＃#.。:：,， \u3000"


def _normalize(text):
    return text.lstrip(_PREFIX_CHARS)


def _plain_text(event) -> str:
    text = ""
    try:
        msg = event.get_message()
        if msg is not None:
            text = msg.extract_plain_text() or ""
    except Exception:
        text = ""
    if not text:
        raw = getattr(getattr(event, "message", None), "content", None)
        raw = raw or getattr(event, "content", "")
        text = str(raw or "")
    return text.strip()


def _session_key(event) -> str:
    gid = getattr(event, "group_openid", None)
    if gid:
        return "group:{0}".format(gid)
    cid = getattr(event, "channel_id", None)
    if cid:
        return "channel:{0}".format(cid)
    return "c2c:{0}".format(_user_id(event))


def _user_id(event) -> str:
    try:
        return str(event.get_user_id())
    except Exception:
        return ""


def _is_admin(uid) -> bool:
    admins = [str(a) for a in _as_list(CFG.get("admins"))]
    return bool(admins) and str(uid) in admins


def _allowed(event):
    """返回 (是否放行, 拒绝原因模板键)。管理员绕过所有限制。"""
    uid = _user_id(event)
    if _is_admin(uid):
        return True, ""

    allowed_users = [str(u) for u in _as_list(CFG.get("allowed_users"))]
    if allowed_users and uid not in allowed_users:
        return False, "permission_denied"

    allowed_groups = [str(g) for g in _as_list(CFG.get("allowed_groups"))]
    gid = getattr(event, "group_openid", None) or getattr(event, "channel_id", None)
    if allowed_groups and gid and str(gid) not in allowed_groups:
        return False, "group_denied"
    return True, ""


# ---------------------------------------------------------------------------
# 指令匹配规则与统一入口
# ---------------------------------------------------------------------------

def _match_command(text):
    """返回 (指令名, 其余参数)；未命中返回 None。

    触发词按长度优先匹配，兼容「扫描 example.com」与「扫描example.com」等写法。
    """
    t = _normalize(text)
    if not t:
        return None
    lowered = t.lower()          # 与 t 等长，仅用于大小写无关比较
    candidates = []
    for name, aliases in _commands().items():
        for alias in aliases:
            a = _normalize(alias).strip().lower()
            if a and lowered.startswith(a):
                candidates.append((len(a), name))
    if not candidates:
        return None
    candidates.sort(reverse=True)          # 最长触发词优先，避免「状态」吃掉「停止x」
    best_len, name = candidates[0]
    return name, t[best_len:].strip()


async def _rule(event) -> bool:
    if not isinstance(event, QQMessageEvent):
        return False
    return _match_command(_plain_text(event)) is not None


matcher = on_message(rule=Rule(_rule), priority=10, block=True)


@matcher.handle()
async def _dispatch(bot: QBot, event: QQMessageEvent, m: Matcher):
    parsed = _match_command(_plain_text(event))
    if not parsed:
        return
    name, rest = parsed

    ok, deny_key = _allowed(event)
    if not ok:
        await m.finish(_tmpl(deny_key))

    handlers = {
        "help": lambda: _cmd_help(m),
        "scan": lambda: _cmd_scan(bot, event, m, rest),
        "stop": lambda: _cmd_stop(event, m, rest),
        "status": lambda: _cmd_status(event, m, rest),
        "tasks": lambda: _cmd_tasks(m),
        "report": lambda: _cmd_report(event, m, rest),
    }
    handler = handlers.get(name)
    if handler is None:
        await m.finish(_tmpl("unknown_command",
                             command=name, help_cmd=_primary("help")))
    try:
        await handler()
    except ZSansApiError as e:
        await m.finish(_tmpl("web_unready", error=str(e)))


# ---------------------------------------------------------------------------
# 指令实现
# ---------------------------------------------------------------------------

async def _cmd_help(m: Matcher):
    c = _commands()
    await m.finish(_tmpl(
        "help",
        help_cmd=c["help"][0],
        scan_cmd=c["scan"][0],
        stop_cmd=c["stop"][0],
        status_cmd=c["status"][0],
        tasks_cmd=c["tasks"][0],
        report_cmd=c["report"][0],
    ))


_DEPTH_RANGE = (1, 10)
_MAX_TARGETS = 20


def _parse_scan_args(rest):
    """解析 scan 参数，返回 (seeds, depth, strategy, errors)。"""
    tokens = rest.split()
    seeds, errors = [], []
    depth = None
    strategy = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("--depth", "-d", "--深度", "深度"):
            if i + 1 < len(tokens):
                depth = tokens[i + 1]
                i += 2
                continue
            errors.append(tok + " 缺少数值")
        elif low.startswith("--depth="):
            depth = tok.split("=", 1)[1]
        elif low in ("--strategy", "--策略", "策略"):
            if i + 1 < len(tokens):
                strategy = tokens[i + 1]
                i += 2
                continue
            errors.append(tok + " 缺少取值")
        else:
            seed, err = classify_target(tok)
            if err is None:
                seeds.append(seed)
            else:
                errors.append(err)
        i += 1

    defaults = CFG.get("defaults") or {}

    if depth is not None:
        try:
            depth = int(float(depth))
        except ValueError:
            errors.append("depth={0} 不是数字".format(depth))
            depth = None
    if depth is None and defaults.get("depth"):
        depth = defaults.get("depth")
    if isinstance(depth, (int, float)):
        depth = int(max(_DEPTH_RANGE[0], min(_DEPTH_RANGE[1], int(depth))))
    else:
        depth = None

    valid_strategies = ("priority_based", "depth_first", "breadth_first", "time_based")
    if strategy is not None and strategy not in valid_strategies:
        errors.append("strategy={0} 无效(可选: {1})".format(strategy, "/".join(valid_strategies)))
        strategy = None
    if strategy is None and defaults.get("strategy") in valid_strategies:
        strategy = str(defaults["strategy"])

    return seeds, depth, strategy, errors


async def _cmd_scan(bot: QBot, event: QQMessageEvent, m: Matcher, rest):
    assert API is not None
    seeds, depth, strategy, errors = _parse_scan_args(rest)

    notes = []
    if errors:
        notes.append(_tmpl("scan_invalid",
                           invalid="\n".join("• " + e for e in errors[:8])))
    if not seeds:
        notes.append(_tmpl("scan_usage", scan_cmd=_primary("scan")))
        await m.finish("\n\n".join(notes))
    if len(seeds) > _MAX_TARGETS:
        seeds = seeds[:_MAX_TARGETS]
        notes.append("(单次最多 {0} 个目标，已截断)".format(_MAX_TARGETS))

    overrides = {}
    if depth is not None:
        overrides["max_depth"] = depth
    if strategy:
        overrides["strategy"] = strategy

    tid = API.start_scan(seeds, overrides or None)

    session = _session_key(event)
    uid = _user_id(event)
    _register_task(session, tid, uid)

    reply = _tmpl(
        "scan_started",
        task_id=tid,
        seeds=", ".join(s.get("value", "") for s in seeds),
        depth=("不限" if depth is None else depth),
        cmd_report=_primary("report"),
    )
    text = "\n\n".join(notes + [reply])

    # 先创建完成跟踪协程再 finish（finish 会抛 FinishedException 结束处理）
    ctx = _SendContext(bot=bot, event=event, session=session)
    asyncio.create_task(_watch_task(ctx, str(tid)))
    logger.info("[qqbot] 任务 %s 由用户 %s(%s) 发起: %s",
                tid, uid, session, ", ".join(s.get("value", "") for s in seeds))
    await m.finish(text)


class _SendContext(object):
    """保存推送报告所需的 bot 与事件引用。"""

    __slots__ = ("bot", "event", "session")

    def __init__(self, bot, event, session):
        self.bot = bot
        self.event = event
        self.session = session


async def _safe_send(ctx, text):
    """尽力发送；平台被动消息时限/风控导致失败时仅记录日志与提示。"""
    try:
        await ctx.bot.send(ctx.event, text)
        return True
    except Exception as e:  # noqa: BLE001 - 推送失败不能拖垮轮询协程
        logger.warning("[qqbot] 报告推送到 %s 失败(被动消息可能已过期): %s",
                       ctx.session, e)
        return False


async def _watch_task(ctx, tid):
    """轮询任务状态直至终态，把报告推送回发起会话。"""
    assert API is not None
    try:
        interval = int(CFG.get("poll_interval") or 5)
    except (TypeError, ValueError):
        interval = 5
    interval = max(1, min(interval, 120))
    try:
        timeout_h = float(CFG.get("scan_timeout_hours") or 24)
    except (TypeError, ValueError):
        timeout_h = 24.0
    deadline = time.time() + max(timeout_h, 0.1) * 3600.0
    notify_on = bool((CFG.get("notify") or {}).get("on_complete", True))

    while time.time() < deadline:
        await asyncio.sleep(interval)
        try:
            t = await asyncio.to_thread(API.get_task, tid)
        except ZSansApiError as e:
            logger.warning("[qqbot] 查询任务 %s 失败: %s", tid, e)
            continue
        status = t.get("status")
        if status == "running":
            continue

        _unregister_task(ctx.session, tid)
        if not notify_on:
            return
        icon = _STATUS_ICON.get(status, "")
        if status == "completed":
            head = _tmpl("notify_complete", summary=_build_summary(t))
        elif status == "stopped":
            head = _tmpl("notify_stopped", task_id=tid,
                         summary=_build_summary(t))
        else:
            head = _tmpl("notify_failed", task_id=tid,
                         error=t.get("error") or "未知错误",
                         summary=_build_summary(t))
        body = "{0} {1}\n{2}".format(icon, head, "").strip("\n") or head
        sent = await _safe_send(ctx, body)
        if not sent:
            hint = _tmpl("notify_send_later",
                         report_cmd=_primary("report"), task_id=tid)
            logger.info("[qqbot] 提示用户可稍后获取报告: %s", hint)
        return

    _unregister_task(ctx.session, tid)
    await _safe_send(ctx, _tmpl("watch_timeout", task_id=tid,
                                hours=int(timeout_h),
                                report_cmd=_primary("report")))


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    mm, ss = divmod(rem, 60)
    if h:
        return "{0}小时{1:02d}分{2:02d}秒".format(h, mm, ss)
    return "{0}分{1:02d}秒".format(mm, ss)


def _build_summary(task):
    """构造报告摘要文本（资产统计 + 样例 + 输出位置）。"""
    assert API is not None
    tid = str(task.get("id", ""))
    status = str(task.get("status", ""))
    metrics = task.get("metrics") or {}
    run_dir = task.get("run_dir") or ""
    pid = os.path.basename(str(run_dir).rstrip("/\\"))

    lines = ["任务 ID: {0}".format(tid)]

    started = task.get("started_at")
    finished = task.get("finished_at")
    if started:
        dur = _fmt_duration((finished or time.time()) - float(started))
        lines.append("耗时: {0}".format(dur))
    lines.append(
        "已处理 {processed} | 新发现 {found} | 深度 {depth} | 错误 {errors}".format(
            processed=metrics.get("assets_processed", "-"),
            found=metrics.get("new_assets_found", "-"),
            depth=metrics.get("depth_reached", "-"),
            errors=metrics.get("errors", 0),
        )
    )

    seeds = task.get("seeds") or []
    if seeds:
        lines.append("目标: " + ", ".join(
            s.get("value", "") if isinstance(s, dict) else str(s) for s in seeds))

    counts = OrderedDict()
    samples = OrderedDict()
    total = 0
    proj = API.get_project(pid) if pid else None
    nodes = (proj or {}).get("nodes") if isinstance(proj, dict) else None
    if isinstance(nodes, list):
        total = len(nodes)
        max_n = int(CFG.get("max_report_assets") or 10)
        for n in nodes:
            ty = str(n.get("type", "other"))
            counts[ty] = counts.get(ty, 0) + 1
            bucket = samples.setdefault(ty, [])
            if len(bucket) < max_n:
                val = n.get("value") or n.get("uid") or ""
                bucket.append(str(val))
    if total:
        stat_line = " | ".join("{0} {1}".format(ty, cnt) for ty, cnt in counts.items())
        lines.append("资产总数: {0}（{1}）".format(total, stat_line))
        for ty, vals in samples.items():
            if vals:
                lines.append("  {ty}: {vals}".format(
                    ty=ty, vals=", ".join(vals[:max_n])[:400]))
    elif status == "completed":
        lines.append("(项目数据尚未导出或为空)")

    if run_dir:
        lines.append("输出目录: {0}".format(run_dir))
    base = str(CFG.get("web_host") or "127.0.0.1")
    port = CFG.get("web_port") or 8050
    lines.append("Web 查看: http://{0}:{1}/".format(base, port))
    return "\n".join(lines)


async def _cmd_stop(event: QQMessageEvent, m: Matcher, rest):
    assert API is not None
    args = rest.split()
    uid = _user_id(event)
    admin = _is_admin(uid)
    tid, err = _lookup_task(_session_key(event),
                            tid=args[0] if args else None,
                            user_id=uid, is_admin=admin)
    if err == "not_found":
        await m.finish(_tmpl("status_not_found", task_id=args[0]))
    if err == "empty" or tid is None:
        await m.finish(_tmpl("stop_usage", stop_cmd=_primary("stop")))

    try:
        t = await asyncio.to_thread(API.get_task, tid)
    except ZSansApiError as e:
        await m.finish(_tmpl("web_unready", error=str(e)))

    if not admin:
        if t.get("status") != "running":
            await m.finish(_tmpl("stop_fail", task_id=tid, reason="任务不在运行中"))
        if not _owns(session_of(event), tid):
            await m.finish(_tmpl("stop_forbidden"))

    stopped = await asyncio.to_thread(API.stop_task, tid)
    if stopped:
        await m.finish(_tmpl("stop_ok", task_id=tid))
    await m.finish(_tmpl("stop_fail", task_id=tid, reason="服务端拒绝(任务可能已结束)"))


def session_of(event):
    return _session_key(event)


def _owns(session_key, tid):
    with _REG_LOCK:
        rows = _SESSION_TASKS.get(session_key) or []
        return any(r["tid"] == str(tid) for r in rows)


async def _cmd_status(event: QQMessageEvent, m: Matcher, rest):
    assert API is not None
    args = rest.split()
    uid = _user_id(event)
    tid, err = _lookup_task(_session_key(event),
                            tid=args[0] if args else None,
                            user_id=uid, is_admin=_is_admin(uid))
    if err == "not_found":
        await m.finish(_tmpl("status_not_found", task_id=args[0]))
    if err == "empty" or tid is None:
        tasks = await asyncio.to_thread(API.list_tasks)
        running = [t for t in tasks if t.get("status") == "running"]
        if running:
            tid = str(running[0].get("id"))
        else:
            await m.finish(_tmpl("tasks_empty"))

    t = await asyncio.to_thread(API.get_task, tid)
    metrics = t.get("metrics") or {}
    started = t.get("started_at")
    elapsed = _fmt_duration(time.time() - float(started)) if started else "-"
    seeds_txt = ", ".join(
        s.get("value", "") if isinstance(s, dict) else str(s)
        for s in (t.get("seeds") or [])) or "-"
    await m.finish(_tmpl(
        "status_line",
        task_id=t.get("id", tid),
        status="{0} {1}".format(_STATUS_ICON.get(t.get("status"), ""), t.get("status")),
        seeds=seeds_txt,
        processed=metrics.get("assets_processed", "-"),
        found=metrics.get("new_assets_found", "-"),
        errors=metrics.get("errors", 0),
        elapsed=elapsed,
    ))


async def _cmd_tasks(m: Matcher):
    assert API is not None
    tasks = await asyncio.to_thread(API.list_tasks)
    if not tasks:
        await m.finish(_tmpl("tasks_empty"))
    header = _tmpl("tasks_header")
    rows = []
    for t in tasks[:10]:
        seeds_txt = ", ".join(
            s.get("value", "") if isinstance(s, dict) else str(s)
            for s in (t.get("seeds") or [])) or "-"
        rows.append(_tmpl("task_row",
                          status="{0} {1}".format(
                              _STATUS_ICON.get(t.get("status"), ""), t.get("status")),
                          task_id=t.get("id", ""),
                          seeds=seeds_txt[:60]))
    await m.finish(header + "\n" + "\n".join(rows))


async def _cmd_report(event: QQMessageEvent, m: Matcher, rest):
    assert API is not None
    args = rest.split()
    uid = _user_id(event)
    tid, err = _lookup_task(_session_key(event),
                            tid=args[0] if args else None,
                            user_id=uid, is_admin=_is_admin(uid))
    if err == "not_found":
        await m.finish(_tmpl("report_not_found", task_id=args[0]))
    if err == "empty" or tid is None:
        await m.finish(_tmpl("report_not_found", task_id=args[0] or "-"))

    t = await asyncio.to_thread(API.get_task, tid)
    status = t.get("status")
    if status == "running":
        await m.finish(_tmpl("report_not_ready", task_id=tid, status=status))
    icon = _STATUS_ICON.get(status, "")
    summary = _build_summary(t)
    if status == "completed":
        head = _tmpl("notify_complete", summary="")
    elif status == "stopped":
        head = _tmpl("notify_stopped", task_id=tid, summary="")
    else:
        head = _tmpl("notify_failed", task_id=tid,
                     error=t.get("error") or "未知错误", summary="")
    await m.finish("{0} {1}\n{2}".format(icon, head.strip(), summary))
