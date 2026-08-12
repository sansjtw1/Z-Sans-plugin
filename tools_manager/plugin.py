"""
外部工具管理器插件(目录式,自包含)
================================
先生成工具下载安装:下载预编译二进制到插件自己的 tools/ 目录并注册进引擎。
本插件与核心模块完全解耦(自带 provision.py,依赖被移除后仍可独立下载)。

本插件采用"目录式"组织, 配置与说明随插件目录走:
    tools_manager/
    ├── plugin.py       # 本入口(on_scan_started / register_cli / plugin_help)
    ├── provision.py    # 自包含的下载安装核心(GitHub Release, 按 OS/架构选包)
    ├── config.yaml     # 插件默认配置(auto_install 等全部可配置项)
    └── README.md       # 插件说明文档

本插件的 CLI 参数由插件自己提供: 插件被加载(启用)后, 通过 register_cli(parser)
向 ``main.py`` 追加 ``--tools`` 参数(接入 --help); 插件未加载或被禁用时,
``--tools`` 不会出现在命令行中。

默认不自动补全(auto_install 缺省为 false), 避免未经许可下载:
  * ``python main.py --tools enable``  开启扫描启动时的自动补全安装
      (写入 plugins.tools_manager.auto_install=true, 并把插件目录写入 plugins.dir)
  * ``python main.py --tools disable``  关闭自动补全

用法:
  python main.py --tools list                                    # 列出工具及安装状态
  python main.py --tools show <包名>                             # 查看单个工具
  python main.py --tools install [包名...]                       # 安装(缺省全部)
  python main.py --tools update  [包名...]                       # 升级到最新
  python main.py --tools remove  [包名...]                       # 卸载
  python main.py --tools enable | disable                        # 开关扫描启动自动补全

配置优先级: 引擎配置(breeding-config.yaml 的 plugins.tools_manager)
覆盖同目录 config.yaml 的默认值; 未配置项回落 config.yaml。
    plugins:
      dir: ""      # 指向本插件所在目录
      tools_manager:
        auto_install: true            # --tools enable 置为 true
        auto_update: false
        dir: tools                    # 下载安装目录(相对本插件目录)
        extra_args:
          ehole: ["-json"]
        enabled_tools:
          - subfinder
          - naabu
          - ehole

环境变量:
    ZSANS_GITHUB_MIRROR               # GitHub 加速镜像前缀(国内网络)
    HTTPS_PROXY / HTTP_PROXY
"""

import argparse
import json
import logging
import os
import re

import yaml

try:
    import provision as _bundled_provision
except ImportError:
    # 兜底:直接以模块路径加载本插件目录下的 provision.py(插件目录已被前置进 sys.path)
    import importlib.util
    import os as _os
    _spec = importlib.util.spec_from_file_location(
        "tools_manager_provision",
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "provision.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _bundled_provision = _mod
provision = _bundled_provision

logger = logging.getLogger("zsans.plugin.tools_manager")

__manifest__ = {
    "name": "tools_manager",
    "version": "3.1.0",
    "description": "外部工具管理器(自包含):subfinder/naabu/EHole,提供 main.py --tools,默认不自动补全",
    "author": "Z-Sans Contributors",
    "conflicts": ["toolbox"],
}

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_bundled_config():
    """读取插件自带 config.yaml 默认值;缺失或损坏时返回空字典。"""
    path = os.path.join(PLUGIN_DIR, "config.yaml")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _plugin_config(engine):
    """合并引擎配置与插件默认配置:引擎 plugins.tools_manager 优先。"""
    cfg = dict(_load_bundled_config())
    plugins_cfg = engine.config.get("plugins") if engine and hasattr(engine, "config") else None
    if isinstance(plugins_cfg, dict):
        override = plugins_cfg.get("tools_manager") or {}
        if isinstance(override, dict):
            cfg.update(override)
    return cfg


def _override_from_config(config_path):
    """从 breed 配置文件读取 plugins.tools_manager 覆盖项(CLI 路径用)。"""
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        plugins_cfg = cfg.get("plugins") or {}
        if isinstance(plugins_cfg, dict):
            override = plugins_cfg.get("tools_manager")
            if isinstance(override, dict):
                return override
    except Exception:
        pass
    return {}


def _cli_plugin_config(config_path):
    """合并默认配置与 breed 配置文件覆盖(CLI 路径用,无需 engine)。"""
    cfg = dict(_load_bundled_config())
    override = _override_from_config(config_path)
    cfg.update(override or {})
    return cfg


def _apply_extra_tools(extra_tools):
    """把配置里的 extra_tools(name -> {repo,bin,label,source,...}) 注册进工具表。

    - 提供 repo 为 GitHub 下载型工具
    - source: path 为系统 PATH / 本地目录工具(仅检测,不下载)
    """
    if not isinstance(extra_tools, dict):
        return
    for name, spec in sorted(extra_tools.items()):
        if not isinstance(spec, dict):
            logger.warning("extra_tools[%s] 不是映射,已跳过", name)
            continue
        extra = {k: v for k, v in spec.items() if k not in ("repo", "bin", "label")}
        provision.add(name, repo=spec.get("repo"), binary=spec.get("bin"),
                      label=spec.get("label"), **extra)


# ---------------------------------------------------------------------------
# CLI: 插件启用即向 main.py 注册 --tools(from register_cli)
# ---------------------------------------------------------------------------

def register_cli(parser):
    """向 main.py 主解析器注册 --tools 参数。

    ``--tools`` 可能已被其它插件(如 toolbox)占用:此时让位,不重复注册,
    以免 argparse 冲突导致本插件加载失败。
    """
    try:
        parser.add_argument(
            "--tools", nargs=argparse.REMAINDER, metavar="CMD ...",
            help="管理外部工具(由 tools_manager 插件提供,默认不自动补全): "
                 "--tools list|show|install|update|remove|enable|disable ...",
        )
        parser.set_defaults(_zplugin_cli=run_cli, _zplugin_active="tools")
    except argparse.ArgumentError as e:
        logger.warning(
            "--tools 已被其它插件(如 toolbox)注册,本插件的 CLI 让位;"
            "可通过 plugins.disable 停用冲突插件后单独使用本插件。%s", e)


def run_cli(args):
    """外部工具管理入口,由 register_cli 的 _zplugin_cli 分发。"""
    tp = argparse.ArgumentParser(prog="main.py --tools", add_help=True)
    sub = tp.add_subparsers(dest="action", required=True)

    sp = sub.add_parser("list", help="列出所有外部工具及安装状态")
    sp.add_argument("--json", action="store_true", help="JSON 输出")

    sp = sub.add_parser("show", help="查看单个工具详情")
    sp.add_argument("pkg")

    sp = sub.add_parser("install", help="安装工具,缺省全部")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--mirror", default=os.environ.get("ZSANS_GITHUB_MIRROR"))
    sp.add_argument("--dir", default=None)

    sp = sub.add_parser("update", help="升级工具到最新,缺省全部")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--mirror", default=os.environ.get("ZSANS_GITHUB_MIRROR"))
    sp.add_argument("--dir", default=None)

    sp = sub.add_parser("remove", help="卸载工具")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--dir", default=None)

    sp = sub.add_parser("enable", help="开启扫描启动时的工具自动补全")
    sp.add_argument("--dir", default=None,
                    help="插件目录(默认本插件所在目录),将写入 config 的 plugins.dir")
    sub.add_parser("disable", help="关闭工具自动补全")

    try:
        a = tp.parse_args(args.tools)
    except SystemExit:
        return 0 if ("-h" in args.tools or "--help" in args.tools) else 2

    tools_dir = a.dir if getattr(a, "dir", None) else None
    if tools_dir is None:
        tools_dir = provision.default_tools_dir(PLUGIN_DIR)

    _apply_extra_tools(_cli_plugin_config(args.config).get("extra_tools") or {})

    if a.action == "list":
        props, (os_name, arch) = provision.status(tools_dir, include_network=True)
        if getattr(a, "json", False):
            print(json.dumps(props, ensure_ascii=False, indent=2))
            return 0
        print("平台: {0}/{1}    安装目录: {2}".format(os_name, arch, tools_dir))
        print("{0:<12} {1:<24} {2}".format("包", "状态", "路径"))
        print("-" * 80)
        for name in sorted(props):
            s = props[name]
            if s["source"] == "path":
                state = "系统 PATH v{ver}".format(ver=s["version"]) if s["installed"] else "未在 PATH 中检测到"
            else:
                state = "已安装 v{ver}".format(ver=s["version"]) if s["installed"] else "未安装"
                if not s["installed"] and s.get("latest"):
                    state += " (最新 v{0})".format(s["latest"])
            print("{0:<12} {1:<24} {2}".format(name, state, s["path"]))
        return 0

    if a.action == "show":
        d = provision.TOOLS.get(a.pkg)
        if d is None:
            logger.error("未知外部工具: %s", a.pkg)
            _hint()
            return 1
        present, ver, path = provision.is_installed(a.pkg, tools_dir)
        os_name, arch = provision.detect_platform()
        print("包: {0}".format(a.pkg))
        print("  用途: {0}".format(provision.TOOLS[a.pkg].get("label", "-")))
        print("  类型: {0}".format("系统 PATH 工具(无需下载/卸载)" if provision.is_path_tool(a.pkg) else "下载型(插件自管)"))
        print("  仓库: {0}".format(provision.TOOLS[a.pkg].get("repo", "-")))
        print("  平台: {0}/{1}".format(os_name, arch))
        print("  状态: {0}".format("已安装 v{0}".format(ver) if present else "未安装"))
        if present:
            print("  路径: {0}".format(path))
        print()
        if provision.is_path_tool(a.pkg):
            print("  说明: PATH 型工具由系统/包管理器提供,核心引擎自动发现,无需安装卸载。")
        else:
            print("  安装: python main.py --tools install {0}".format(a.pkg))
        return 0

    def _targets(pkgs):
        if not pkgs:
            return sorted(provision.TOOLS)
        bad = [p for p in pkgs if p not in provision.TOOLS]
        if bad:
            logger.error("未知外部工具: %s", ", ".join(bad))
            _hint()
            return None
        return list(pkgs)

    def _do(action):
        targets = _targets(a.pkgs)
        if targets is None:
            return 1
        ok = True
        for name in targets:
            try:
                if action == "install":
                    path, ver = provision.install(name, tools_dir, mirror=a.mirror)
                    print("→ 已安装 {0} v{1}: {2}".format(name, ver, path))
                elif action == "update":
                    path, ver = provision.install(name, tools_dir, force=True, mirror=a.mirror)
                    print("→ 已更新 {0} → v{1}: {2}".format(name, ver, path))
                elif action == "remove":
                    removed = provision.remove(name, tools_dir)
                    if provision.is_path_tool(name) and not removed:
                        print("→ {0} 为系统 PATH 工具,无需卸载".format(name))
                    else:
                        print("→ 已卸载 {0}".format(name))
            except Exception as e:
                logger.debug("工具管理操作 %s 异常", action, exc_info=e)
                print("✗ {0}: {1}".format(action, e))
                ok = False
        return 0 if ok else 1

    if a.action in ("install", "update", "remove"):
        return _do(a.action)

    if a.action == "enable":
        plugin_dir = a.dir if getattr(a, "dir", None) else PLUGIN_DIR
        oks = []
        oks.append(_set_auto_install(True, args.config))
        oks.append(_set_plugins_dir(plugin_dir, args.config))
        if all(ok for ok, _ in oks):
            print("→ 已开启扫描启动时的工具自动补全(工具缺失时自动下载)。")
            print("  立即安装可用: python main.py --tools install")
            return 0
        for ok, msg in oks:
            print("→ " + msg if ok else "✗ " + msg)
        return 1

    if a.action == "disable":
        ok, msg = _set_auto_install(False, args.config)
        print("→ " + msg if ok else "✗ " + msg)
        return 0 if ok else 1

    return 0


def _hint():
    print("可用外部工具: {0}".format(", ".join(sorted(provision.TOOLS))))


def _set_yaml_field(raw, block, field, value):
    """文本级把 YAML 块 block 下的 field 置为 value,保留注释。"""
    lines = raw.splitlines()
    block_indent = None
    block_end = None
    field_line = None
    field_indent = None
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        bi = len(line) - len(line.lstrip())
        m = re.match(r'^\s*' + re.escape(block) + r':\s*(#.*)?$', line)
        if m and block_indent is None:
            block_indent = bi
            # 找块结束位置
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip():
                    continue
                if len(nxt) - len(nxt.lstrip()) <= bi:
                    end = j
                    break
            block_end = end
            for j in range(idx + 1, end):
                nxt = lines[j]
                if not nxt.strip():
                    continue
                if len(nxt) - len(nxt.lstrip()) <= bi:
                    break
                if re.match(r'^\s+' + re.escape(field) + r':', nxt):
                    field_line = j
                    field_indent = len(nxt) - len(nxt.lstrip())
                    break
            break

    if field_line is not None:
        return "\n".join(
            lines[:field_line]
            + [" " * field_indent + "{f}: {v}".format(f=field, v=value)]
            + lines[field_line + 1:]
        )
    if block_indent is not None:
        return "\n".join(
            lines[:block_end]
            + [" " * (block_indent + 2) + "{f}: {v}".format(f=field, v=value)]
            + lines[block_end:]
        )
    out = raw.rstrip('\n')
    if out:
        out += "\n"
    out += "{b}:\n  {f}: {v}\n".format(b=block, f=field, v=value)
    return out


def _read_config(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def _write_config(config_path, raw):
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(raw)


def _set_auto_install(enabled, config_path):
    value = "true" if enabled else "false"
    raw = _read_config(config_path)
    if raw is None:
        raw = "plugins:\n"
    if "tools_manager:" not in raw:
        raw = _set_yaml_field(raw, "plugins", "tools_manager", "")
    raw = _set_yaml_field(raw, "tools_manager", "auto_install", value)
    try:
        _write_config(config_path, raw)
    except OSError as e:
        return False, "无法写入配置: {e}".format(e=e)
    return True, "tools_manager.auto_install={value}".format(value=value)


def _set_plugins_dir(dir_path, config_path):
    quoted = '"{0}"'.format(dir_path) if " " in dir_path else dir_path
    raw = _read_config(config_path)
    if raw is None:
        raw = "plugins:\n"
    raw = _set_yaml_field(raw, "plugins", "dir", quoted)
    try:
        _write_config(config_path, raw)
    except OSError as e:
        return False, "无法写入配置: {e}".format(e=e)
    return True, "plugins.dir={quoted}".format(quoted=quoted)


# ---------------------------------------------------------------------------
# 扫描钩子
# ---------------------------------------------------------------------------

def _as_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def on_scan_started(engine):
    """扫描启动钩子:仅当配置开启(auto_install)时下载缺失工具并注册进引擎。"""
    orchestrator = getattr(engine, "tool_orchestrator", None)
    if orchestrator is None:
        logger.warning("引擎未初始化 tool_orchestrator, 工具自动补全已跳过")
        return

    pcfg = _plugin_config(engine)
    auto_install = _as_bool(pcfg.get("auto_install", False))
    auto_update = _as_bool(pcfg.get("auto_update", False))
    tools_dir = provision.resolve_tools_dir(PLUGIN_DIR, pcfg.get("dir"))
    extra_args = pcfg.get("extra_args") or {}
    if not isinstance(extra_args, dict):
        extra_args = {}
    _apply_extra_tools(pcfg.get("extra_tools") or {})

    enabled = pcfg.get("enabled_tools")
    targets = [str(t) for t in enabled] if enabled else sorted(provision.TOOLS)

    for name in targets:
        if name not in provision.TOOLS:
            logger.warning("未知外部工具 %s,已跳过", name)
            continue
        label = provision.TOOLS[name].get("label", name)
        installed, ver, path = provision.is_installed(name, tools_dir)
        if provision.is_path_tool(name):
            if installed:
                orchestrator.register_tool(name, path=path, version=ver, extra_args=extra_args.get(name))
                logger.info("外部工具 %s(%s) 经系统 PATH 发现: %s", name, label, path)
            else:
                logger.info("外部工具 %s(%s) 为系统 PATH 工具,未检测到,交由核心引擎回退处理", name, label)
            continue
        if not installed:
            if not auto_install:
                logger.info("外部工具 %s(%s) 未安装且自动补全未开启,跳过", name, label)
                continue
            logger.info("外部工具 %s(%s) 缺失,自动补全安装...", name, label)
            try:
                path, ver = provision.install(name, tools_dir)
            except Exception as e:
                logger.warning("自动补全安装 %s 失败(回退内置方法): %s", name, e)
                continue
        elif auto_update:
            logger.info("外部工具 %s(%s) 检查更新(当前 %s)...", name, label, ver)
            try:
                path, ver = provision.install(name, tools_dir, force=True)
            except Exception as e:
                logger.warning("更新 %s 失败(沿用旧版): %s", name, e)

        orchestrator.register_tool(name, path=path, version=ver, extra_args=extra_args.get(name))
        logger.info("外部工具 %s(%s) 就绪: %s", name, ver, path)


def on_scan_completed(engine):
    """扫描结束钩子:写一份工具环境报告到本次运行目录。"""
    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not outdir:
        return
    pcfg = _plugin_config(engine)
    tools_dir = provision.resolve_tools_dir(PLUGIN_DIR, pcfg.get("dir"))
    os_name, arch = provision.detect_platform()
    _apply_extra_tools(pcfg.get("extra_tools") or {})
    report = {
        "插件": "tools_manager",
        "平台": "{os}/{arch}".format(os=os_name, arch=arch),
        "安装目录": tools_dir,
        "工具": {},
    }
    for name in sorted(provision.TOOLS):
        present, ver, path = provision.is_installed(name, tools_dir)
        report["工具"][name] = {
            "用途": provision.TOOLS[name].get("label", ""),
            "已安装": present,
            "版本": ver,
            "路径": path,
        }
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "tools_environment_report.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("工具环境报告: %s", path)
    except Exception as e:
        logger.warning("工具环境报告写入失败: %s", e)


def plugin_help():
    """供 ``python main.py --plugin-info tools_manager`` 调用的动态帮助。"""
    lines = []
    lines.append("tools_manager - 外部工具管理器(自包含下载,不依赖核心安装器)")
    lines.append("=" * 60)
    lines.append("外部工具({n}):".format(n=len(provision.TOOLS)))
    for name in sorted(provision.TOOLS):
        d = provision.TOOLS[name]
        kind = "PATH" if d.get("source") == "path" else "下载"
        lines.append("  {name:<10} {label}  ({kind}, repo: {repo})".format(
            name=name, label=d.get("label", ""), kind=kind, repo=d.get("repo", "-")))
    lines.append("")
    lines.append("本插件被加载(启用)后, 才向 main.py 提供 --tools 参数:")
    lines.append("  python main.py --tools list                        # 查看外部工具状态")
    lines.append("  python main.py --tools show <包名>                 # 查看单个工具")
    lines.append("  python main.py --tools install   [包名...]         # 安装(缺省全部)")
    lines.append("  python main.py --tools update    [包名...]         # 升级到最新")
    lines.append("  python main.py --tools remove    [包名...]         # 卸载")
    lines.append("  python main.py --tools enable                     # 开启扫描启动时的自动补全")
    lines.append("  python main.py --tools disable                    # 关闭自动补全")
    lines.append("")
    lines.append("下载目录: {0}".format(provision.default_tools_dir(PLUGIN_DIR)))
    lines.append("国内下载加速: export ZSANS_GITHUB_MIRROR=https://gh-proxy.com")
    lines.append("")
    lines.append("当前状态:")
    tools_dir = provision.default_tools_dir(PLUGIN_DIR)
    for name in sorted(provision.TOOLS):
        present, ver, _ = provision.is_installed(name, tools_dir)
        mark = "已安装 v{ver}".format(ver=ver) if present else "未安装"
        lines.append("  {name:<10} {mark}".format(name=name, mark=mark))
    return "\n".join(lines)