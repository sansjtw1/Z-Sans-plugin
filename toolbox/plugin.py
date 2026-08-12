"""toolbox — a general-purpose, international external-tool manager (directory plugin).

Manages the external binaries the scan engine relies on (subfinder / naabu /
EHole) plus any number of extra GitHub-released tools registered via
``plugins.toolbox.extra_tools``. Fully self-contained: English UI, English
docs, no dependency on the project's Chinese localisation.

CLI (apt-style, only present while the plugin is loaded):
    python main.py --tools list|show|install|update|remove|enable|disable ...

Behaviour:
    * Nothing is downloaded without permission (``auto_install`` defaults to
      false). Run ``--tools enable`` to switch on provisioning of missing
      tools when a scan starts.
    * ``on_scan_started`` installs/refreshes tools and registers their paths
      with ``engine.tool_orchestrator``.
    * ``on_scan_completed`` writes ``tools_environment_report.json`` into the
      current run directory.

Configuration precedence: engine config (``plugins.toolbox`` in the breed
config file) overrides this plugin's bundled ``config.yaml`` defaults.
"""

import argparse
import json
import logging
import os
import re

import yaml

try:
    from . import provision
except ImportError:
    import provision  # allow running directly from the plugin folder

logger = logging.getLogger("zsans.plugin.toolbox")

__manifest__ = {
    "name": "toolbox",
    "version": "1.0.0",
    "description": "General-purpose external tool manager (subfinder/naabu/EHole + extra tools); provides main.py --tools",
    "author": "Z-Sans Contributors",
    "conflicts": ["tools_manager"],
}

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(PLUGIN_DIR)  # directory that CONTAINS this plugin


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_bundled_config():
    path = os.path.join(PLUGIN_DIR, "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _override_from_config(config_path):
    """Read ``plugins.toolbox`` straight from the breed config file (used by
    the CLI path; the engine hook path uses ``engine.config`` instead)."""
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh.read()) or {}
        pc = data.get("plugins") or {}
        if isinstance(pc, dict):
            override = pc.get("toolbox") or {}
            return override if isinstance(override, dict) else {}
    except Exception:
        return {}
    return {}


def _effective(base, override):
    merged = dict(base or {})
    if isinstance(override, dict):
        merged.update(override)
    return merged


def _as_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _apply_extra_tools(extra_tools):
    """Register additional tools (name -> {repo, bin, label, ...}) at runtime."""
    if not isinstance(extra_tools, dict):
        return
    for name, spec in sorted(extra_tools.items()):
        if not isinstance(spec, dict):
            logger.warning("extra_tools[%s] is not a mapping, skipped", name)
            continue
        extra = {k: v for k, v in spec.items() if k not in ("repo", "bin", "label")}
        provision.register(name, repo=spec.get("repo"), binary=spec.get("bin"),
                           label=spec.get("label"), **extra)


def _plugin_config(config_path=None, engine=None):
    """Merge bundled defaults with engine (or file) overrides."""
    base = _load_bundled_config()
    if engine is not None:
        pc = getattr(engine, "config", {}).get("plugins") or {}
        override = pc.get("toolbox") if isinstance(pc, dict) else None
    else:
        override = _override_from_config(config_path)
    return _effective(base, override)


# ---------------------------------------------------------------------------
# CLI: --tools (registered only while the plugin is loaded)
# ---------------------------------------------------------------------------

def register_cli(parser):
    parser.add_argument(
        "--tools", nargs=argparse.REMAINDER, metavar="CMD ...",
        help="Manage external tools (apt-style, provided by the toolbox plugin): "
             "--tools list|show|install|update|remove|enable|disable ...",
    )
    parser.set_defaults(_zplugin_cli=run_cli, _zplugin_active="tools")


def _hint():
    print("Available packages: {0}".format(", ".join(sorted(provision.TOOLS))))


def _targets(pkgs):
    if not pkgs:
        return sorted(provision.TOOLS)
    bad = [p for p in pkgs if p not in provision.TOOLS]
    if bad:
        logger.error("unknown package(s): %s", ", ".join(bad))
        _hint()
        return None
    return list(pkgs)


def _tools_dir_from_cfg(cfg):
    return provision.resolve_tools_dir(PLUGIN_DIR, cfg.get("dir"))


def run_cli(args):
    cfg = _plugin_config(config_path=getattr(args, "config", None))
    _apply_extra_tools(cfg.get("extra_tools") or {})
    tools_dir = _tools_dir_from_cfg(cfg)
    mirror = cfg.get("mirror") or os.environ.get("ZSANS_GITHUB_MIRROR")

    tp = argparse.ArgumentParser(prog="main.py --tools", add_help=True)
    sub = tp.add_subparsers(dest="action", required=True)

    sp = sub.add_parser("list", help="List all tools and their install status")
    sp.add_argument("--json", action="store_true", help="JSON output")

    sp = sub.add_parser("show", help="Show details for a single tool")
    sp.add_argument("pkg")
    sp.add_argument("--json", action="store_true", help="JSON output")

    sp = sub.add_parser("install", help="Install tools (all by default)")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--mirror", default=mirror, help="GitHub mirror prefix")
    sp.add_argument("--dir", default=None, help="Install directory (default: <plugin>/tools)")

    sp = sub.add_parser("update", help="Update tools to their latest version")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--mirror", default=mirror, help="GitHub mirror prefix")
    sp.add_argument("--dir", default=None, help="Install directory")

    sp = sub.add_parser("remove", help="Remove tools")
    sp.add_argument("pkgs", nargs="*", metavar="PKG")
    sp.add_argument("--dir", default=None, help="Install directory")

    sp = sub.add_parser("enable", help="Enable automatic provisioning of missing tools at scan start")
    sp.add_argument("--dir", default=None, help="Plugin container dir to write into plugins.dir")
    sub.add_parser("disable", help="Disable automatic provisioning")

    try:
        a = tp.parse_args(args.tools)
    except SystemExit:
        return 0 if ("-h" in args.tools or "--help" in args.tools) else 2

    install_dir = a.dir if getattr(a, "dir", None) else tools_dir

    if a.action == "list":
        props, (os_name, arch) = provision.status(install_dir, include_network=False)
        if getattr(a, "json", False):
            print(json.dumps(props, ensure_ascii=False, indent=2))
            return 0
        print("Platform: {0}/{1}".format(os_name, arch))
        print("{0:<12} {1:<28} {2}".format("PACKAGE", "STATUS", "PATH"))
        print("-" * 78)
        for name in sorted(props):
            s = props[name]
            if s["source"] == "path":
                state = "system PATH v{0}".format(s["version"]) if s["installed"] else "not on PATH"
            else:
                state = "installed {0}".format(s["version"]) if s["installed"] else "not installed"
            print("{0:<12} {1:<28} {2}".format(name, state, s["path"]))
        return 0

    if a.action == "show":
        if a.pkg not in provision.TOOLS:
            logger.error("unknown package: %s", a.pkg)
            _hint()
            return 1
        d = provision.TOOLS[a.pkg]
        present, ver, path = provision.is_installed(a.pkg, install_dir)
        os_name, arch = provision.detect_platform()
        out = {
            "pkg": a.pkg,
            "purpose": d.get("label", "-"),
            "type": "system PATH (no download/uninstall)" if provision.is_path_tool(a.pkg) else "download (plugin-managed)",
            "repo": d.get("repo", "-"),
            "platform": "{0}/{1}".format(os_name, arch),
            "status": "system PATH v{0}".format(ver) if present else "not installed/not on PATH",
            "path": path if present else None,
        }
        if getattr(a, "json", False):
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        for k, v in out.items():
            print("{0:<12} {1}".format(k.upper(), v))
        return 0

    def _do(action):
        targets = _targets(a.pkgs)
        if targets is None:
            return 1
        ok = True
        for name in targets:
            try:
                if action == "install":
                    path, ver = provision.install(name, install_dir, mirror=a.mirror)
                    print("[installed] {0} {1}: {2}".format(name, ver, path))
                elif action == "update":
                    path, ver = provision.install(name, install_dir, force=True, mirror=a.mirror)
                    print("[updated] {0} -> {1}: {2}".format(name, ver, path))
                elif action == "remove":
                    removed = provision.remove(name, install_dir)
                    if provision.is_path_tool(name) and not removed:
                        print("[skip] {0} is a system PATH tool; nothing to remove".format(name))
                    else:
                        print("[removed] {0}".format(name) if removed else "[missing] {0}".format(name))
            except Exception as e:
                logger.debug("%s failed for %s", action, name, exc_info=e)
                print("[error] {0}: {1}".format(action, e))
                ok = False
        return 0 if ok else 1

    if a.action in ("install", "update", "remove"):
        return _do(a.action)

    conf = getattr(args, "config", None)

    if a.action == "enable":
        plugin_root = a.dir if getattr(a, "dir", None) else PLUGIN_ROOT
        results = [
            _set_yaml(conf, "plugins", "dir", _quote(plugin_root)),
            _set_yaml(conf, "toolbox", "auto_install", "true"),
        ]
        if all(ok for ok, _ in results):
            print("[enabled] tools will be provisioned automatically when a scan starts.")
            print("          install now with:  python main.py --tools install")
            return 0
        for ok, msg in results:
            print("[error] " + msg if not ok else msg)
        return 1

    if a.action == "disable":
        ok, msg = _set_yaml(conf, "toolbox", "auto_install", "false")
        print("[disabled]" if ok else "[error] " + msg)
        return 0 if ok else 1

    return 0


# ---------------------------------------------------------------------------
# Text-level YAML editing (keeps comments, minimal diffs)
# ---------------------------------------------------------------------------

def _read_config(config_path):
    if not config_path or not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _write_config(config_path, raw):
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(raw)


def _quote(path):
    return '"{0}"'.format(path) if " " in str(path) else str(path)


def _indent_of(line):
    return len(line) - len(line.lstrip())


def _find_block(lines, name):
    """Return (line_index, indent) of the first ``name:`` block, preferring the
    outermost (smallest indent) occurrence. None if absent."""
    best = None
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        m = re.match(r'^(\s*){0}:\s*(#.*)?$'.format(re.escape(name)), ln)
        if m and (best is None or len(m.group(1)) < best[1]):
            best = (i, len(m.group(1)))
    return best


def _block_end(lines, start):
    """Index of the first line after the block starting at ``start``."""
    base = _indent_of(lines[start])
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        if _indent_of(ln) <= base:
            return j
    return len(lines)


def _append_block(lines, parent, name):
    """Append ``name:`` (nested under ``parent`` when given) as a new block."""
    if parent is None:
        return lines + ["{0}:".format(name)]
    p = _find_block(lines, parent)
    if p is None:
        return lines + ["{0}:".format(parent), "  {0}:".format(name)]
    start, indent = p
    end = _block_end(lines, start)
    insert = " " * (indent + 2) + "{0}:".format(name)
    return lines[:end] + [insert] + lines[end:]


def _set_yaml(config_path, block, field, value):
    """Set ``field`` inside the YAML ``block`` (matched by leaf name) of the
    config file, preserving every other line and its comments. The block is
    appended if missing (``toolbox`` is created under ``plugins``)."""
    raw = _read_config(config_path)
    if raw is None:
        raw = "plugins:\n"
        try:
            _write_config(config_path, raw)
        except OSError as e:
            return False, "cannot write config: {0}".format(e)

    lines = raw.splitlines()
    found = _find_block(lines, block)
    if found is None:
        lines = _append_block(lines, "plugins" if block == "toolbox" else None, block)
        found = _find_block(lines, block)
        if found is None:
            return False, "cannot create block: {0}".format(block)

    start, indent = found
    end = _block_end(lines, start)
    field_line = None
    field_indent = None
    for j in range(start + 1, end):
        ln = lines[j]
        if not ln.strip():
            continue
        if _indent_of(ln) <= indent:
            break
        if re.match(r'^\s*{0}:'.format(re.escape(field)), ln):
            field_line = j
            field_indent = _indent_of(ln)
            break

    if field_line is not None:
        m = re.search(r'(#.*)$', lines[field_line])
        suffix = (" " + m.group(1)) if m else ""
        lines[field_line] = " " * field_indent + "{0}: {1}{2}".format(field, value, suffix)
        out = "\n".join(lines)
    else:
        insert_indent = " " * (indent + 2)
        out = "\n".join(lines[:end] + ["{0}{1}: {2}".format(insert_indent, field, value)] + lines[end:])

    try:
        _write_config(config_path, out)
    except OSError as e:
        return False, "cannot write config: {0}".format(e)
    return True, "{0}.{1}={2}".format(block, field, value)


# ---------------------------------------------------------------------------
# Scan hooks
# ---------------------------------------------------------------------------

def on_scan_started(engine):
    """Provision missing tools (when enabled) and register them with the
    engine's tool orchestrator before breeding starts."""
    cfg = _plugin_config(engine=engine)
    _apply_extra_tools(cfg.get("extra_tools") or {})
    tools_dir = _tools_dir_from_cfg(cfg)
    mirror = cfg.get("mirror") or os.environ.get("ZSANS_GITHUB_MIRROR")

    orchestrator = getattr(engine, "tool_orchestrator", None)
    if orchestrator is None:
        logger.warning("engine has no tool_orchestrator; toolbox provisioning skipped")
        return

    auto_install = _as_bool(cfg.get("auto_install", False))
    auto_update = _as_bool(cfg.get("auto_update", False))
    extra_args = cfg.get("extra_args") or {}
    if not isinstance(extra_args, dict):
        extra_args = {}

    enabled = pcfg.get("enabled_tools")
    targets = [str(t) for t in enabled] if enabled else sorted(provision.TOOLS)

    for name in targets:
        if name not in provision.TOOLS:
            logger.warning("unknown tool %s, skipped", name)
            continue
        installed, ver, path = provision.is_installed(name, tools_dir)
        if provision.is_path_tool(name):
            if installed:
                orchestrator.register_tool(name, path=path, version=ver, extra_args=extra_args.get(name))
                logger.info("tool %s (%s) found on system PATH: %s", name,
                            provision.TOOLS[name].get("label", name), path)
            else:
                logger.info("tool %s (%s) is a system PATH tool but not detected; left to engine fallback",
                            name, provision.TOOLS[name].get("label", name))
            continue
        if not installed:
            if not auto_install:
                logger.info("tool %s (%s) not installed and auto_install is off; skipped",
                            name, provision.TOOLS[name].get("label", name))
                continue
            logger.info("tool %s (%s) missing, provisioning...", name, provision.TOOLS[name].get("label", name))
            try:
                path, ver = provision.install(name, tools_dir, mirror=mirror)
            except Exception as e:
                logger.warning("provisioning %s failed (engine falls back to built-ins): %s", name, e)
                continue
        elif auto_update:
            logger.info("tool %s (%s) checking for updates (current %s)...", name,
                        provision.TOOLS[name].get("label", name), ver)
            try:
                path, ver = provision.install(name, tools_dir, force=True, mirror=mirror)
            except Exception as e:
                logger.warning("updating %s failed (keeping old version): %s", name, e)

        orchestrator.register_tool(name, path=path, version=ver, extra_args=extra_args.get(name))
        logger.info("tool %s (%s) ready: %s", name, ver, path)


def on_scan_completed(engine):
    """Write a tool-environment report (English) into the current run dir."""
    cfg = _plugin_config(engine=engine)
    _apply_extra_tools(cfg.get("extra_tools") or {})
    tools_dir = _tools_dir_from_cfg(cfg)

    handler = getattr(engine, "output_handler", None)
    outdir = getattr(handler, "run_dir", None) or getattr(handler, "output_dir", None)
    if not outdir:
        return
    os_name, arch = provision.detect_platform()
    report = {
        "plugin": "toolbox",
        "platform": "{0}/{1}".format(os_name, arch),
        "tools": {},
    }
    for name in sorted(provision.TOOLS):
        present, ver, path = provision.is_installed(name, tools_dir)
        report["tools"][name] = {
            "purpose": provision.TOOLS[name].get("label", ""),
            "installed": present,
            "version": ver,
            "path": path,
        }
    try:
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "tools_environment_report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info("tool environment report written: %s", path)
    except Exception as e:
        logger.warning("failed to write tool environment report: %s", e)


def plugin_help():
    """Dynamic help shown by ``python main.py --plugin-info toolbox``."""
    lines = []
    lines.append("toolbox - General-purpose external tool manager")
    lines.append("=" * 56)
    lines.append("Tools under management ({0}):".format(len(provision.TOOLS)))
    for name in sorted(provision.TOOLS):
        d = provision.TOOLS[name]
        lines.append("  {name:<12} {label}  (repo: {repo})".format(
            name=name, label=d.get("label", ""), repo=d.get("repo", "-")))
    lines.append("")
    lines.append("This plugin only provides the --tools CLI while it is loaded:")
    lines.append("  python main.py --tools list                    # tool status")
    lines.append("  python main.py --tools show <pkg>              # details")
    lines.append("  python main.py --tools install   [pkg ...]     # install (all by default)")
    lines.append("  python main.py --tools update    [pkg ...]     # update to latest")
    lines.append("  python main.py --tools remove    [pkg ...]     # uninstall")
    lines.append("  python main.py --tools enable                 # auto-provision at scan start")
    lines.append("  python main.py --tools disable                # disable auto-provisioning")
    lines.append("")
    lines.append("Extra tools can be added under plugins.toolbox.extra_tools in the breed config.")
    lines.append("A mirror for GitHub downloads: export ZSANS_GITHUB_MIRROR=https://gh-proxy.com")
    lines.append("")
    lines.append("Current status:")
    tools_dir = _tools_dir_from_cfg(_plugin_config())
    for name in sorted(provision.TOOLS):
        present, ver, _ = provision.is_installed(name, tools_dir)
        mark = "installed {0}".format(ver) if present else "not installed"
        lines.append("  {name:<12} {mark}".format(name=name, mark=mark))
    return "\n".join(lines)