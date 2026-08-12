# coding: utf-8
"""外部工具下载安装核心逻辑(tools_manager 插件自包含版)。

与核心模块完全解耦(核心已移除跨平台安装器);本插件自带下载能力,走 GitHub
Release 匹配当前 OS/架构下载预编译二进制,安装到插件自己的 tools/ 目录并用
manifest.json 记录。可动态注册新工具(add)。

支持的平台: linux / darwin / windows × amd64 / arm64 / 386 / arm。
内置工具: subfinder(子域名) / naabu(端口) / EHole(Web指纹,保留指纹库)。
"""

import json
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import zipfile

import requests

logger = logging.getLogger("zsans.plugin.tools_manager.provision")


TOOLS = {
    "subfinder": {
        "repo": "projectdiscovery/subfinder",
        "bin": "subfinder",
        "label": "子域名发现",
    },
    "naabu": {
        "repo": "projectdiscovery/naabu",
        "bin": "naabu",
        "label": "端口扫描",
    },
    "ehole": {
        "repo": "EdgeSecurityTeam/EHole",
        "bin": "EHole",
        "label": "Web指纹识别",
        # 解压后保留全部文件(内含 finger.json 指纹库,可执行文件需与其同目录)
        "keep_extras": True,
    },
    "whatweb": {
        # PATH 型工具:不下载,由核心引擎经系统 PATH / 配置路径发现
        "bin": "whatweb",
        "label": "Web指纹识别(扩展,走系统 PATH)",
        "source": "path",
    },
}

_OS_TOKENS = {
    "linux": ("linux",),
    "darwin": ("darwin", "macos", "osx"),
    "windows": ("windows",),
}
_ARCH_TOKENS = {
    "amd64": ("amd64", "x86_64"),
    "arm64": ("arm64", "aarch64"),
    "386": ("386", "x86", "i686"),
    "arm": ("arm",),
}
_TOKEN_RE = {}
_IGNORED_EXT = {".json", ".txt", ".md", ".cfg", ".ini", ".yml", ".yaml", ".sh", ".example", ".sbom", ".sig", ".zip"}


def add(name, repo=None, binary=None, label=None, **kwargs):
    """动态注册 / 覆盖一个外部工具定义(供插件扩展)。

    - repo:   GitHub 仓库全名, 如 "projectdiscovery/httpx"
    - binary: 可执行文件统一名(不带 .exe)
    - label:  中文用途说明
    """
    name = str(name)
    existing = name in TOOLS
    d = dict(TOOLS.get(name, {}))
    if repo:
        d["repo"] = repo
    if binary:
        d["bin"] = binary
    if label:
        d["label"] = label
    d.update(kwargs)
    TOOLS[name] = d
    logger.info("已注册工具定义: %s (repo=%s)", name, d.get("repo"))
    return not existing


def detect_platform():
    """返回 (os_name, arch)，如 ("linux", "amd64")。"""
    raw = platform.system().lower()
    if raw.startswith("win"):
        os_name = "windows"
    elif raw == "darwin":
        os_name = "darwin"
    else:
        os_name = "linux"

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    elif machine.startswith("arm"):
        arch = "arm"
    else:
        arch = "amd64"
    return os_name, arch


def exe_name(binary):
    """按平台给可执行文件补充 .exe 后缀。"""
    return binary + (".exe" if sys.platform.startswith("win") else "")


def path_tool_version(exe_path):
    """运行 '<exe> --version' 提取首个版本号,失败返回 None。"""
    try:
        import subprocess
        out = subprocess.run([exe_path, "--version"], capture_output=True, text=True, timeout=15)
        text = (out.stdout or out.stderr or "").strip()
        m = re.search(r"\d+(?:\.\d+)+", text)
        return m.group(0) if m else (text.splitlines()[0] if text else None)
    except Exception:
        return None


def is_path_tool(name):
    """该工具是否为 PATH 型(系统自带/包管理安装,而非下载型)。"""
    d = TOOLS.get(name) or {}
    return bool(d.get("source") == "path")


def locate_path_tool(name, tools_dir):
    """PATH 型工具查找,与核心引擎发现链保持一致。

    顺序: 本地目录(多架构 tools/<name>/{os}/{arch} → tools/<name> 单文件)
    → assets/ → 系统 PATH;入参 tools_dir(插件自身下载目录)也会被搜索。
    返回 (可执行文件绝对路径, 版本号),未找到返回 (None, None)。
    """
    os_name, arch = detect_platform()
    exe = exe_name(name)
    search_dirs = []
    for d in (tools_dir, os.path.join(os.getcwd(), "tools"), "assets"):
        if d and not os.path.isabs(d):
            d = os.path.abspath(d)
        search_dirs.append(d)
    for d in search_dirs:
        cands = [
            os.path.join(d, name, os_name, arch, exe),
            os.path.join(d, name, exe),
        ]
        for cand in cands:
            if os.path.isfile(cand):
                return os.path.abspath(cand), path_tool_version(cand)
    found = shutil.which(name)
    if found:
        return found, path_tool_version(found)
    return None, None


def default_tools_dir(plugin_dir):
    """默认安装目录: <插件目录>/tools。"""
    return os.path.join(plugin_dir, "tools")


def resolve_tools_dir(plugin_dir, cfg_dir):
    """解析配置的安装目录:绝对路径直接透传,相对路径相对插件目录解释。"""
    if not cfg_dir:
        return default_tools_dir(plugin_dir)
    if os.path.isabs(cfg_dir):
        return cfg_dir
    return os.path.join(plugin_dir, cfg_dir)


def manifest_path(tools_dir):
    return os.path.join(tools_dir, "manifest.json")


def read_manifest(tools_dir):
    try:
        with open(manifest_path(tools_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_manifest(data, tools_dir):
    os.makedirs(os.path.dirname(manifest_path(tools_dir)), exist_ok=True)
    with open(manifest_path(tools_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _mirrorize(url, mirror):
    if not mirror:
        return url
    mirror = mirror.rstrip("/")
    if url.startswith(mirror):
        return url
    return "{mirror}/{url}".format(mirror=mirror, url=url)


def _has_token(name, token):
    r = _TOKEN_RE.get(token)
    if r is None:
        r = re.compile(r"(?<![a-z0-9]){0}(?![a-z0-9])".format(re.escape(token)))
        _TOKEN_RE[token] = r
    return r.search(name) is not None


def latest_release(repo, mirror=None, timeout=20):
    """查询 GitHub 最新 release，返回 (version, [assets])。"""
    url = "https://api.github.com/repos/{repo}/releases/latest".format(repo=repo)
    resp = requests.get(_mirrorize(url, mirror), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    version = str(data.get("tag_name", "")).lstrip("v")
    assets = [
        {"name": a.get("name", ""), "url": a.get("browser_download_url", ""), "size": a.get("size", 0)}
        for a in data.get("assets", [])
    ]
    return version, assets


def match_asset(assets, os_name, arch):
    """从发布资产里挑出匹配当前平台/架构的压缩包，找不到返回 None。"""
    os_tokens = _OS_TOKENS.get(os_name, ("linux",))
    arch_tokens = _ARCH_TOKENS.get(arch, ("amd64",))
    for a in assets:
        n = a["name"].lower()
        if "checksum" in n or ".sig" in n:
            continue
        if not (n.endswith(".zip") or n.endswith(".tar.gz")):
            continue
        if not any(_has_token(n, t) for t in os_tokens):
            continue
        if not any(_has_token(n, t) for t in arch_tokens):
            continue
        return a
    return None


def _download(url, dest, timeout=240):
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=131072):
                    if chunk:
                        fh.write(chunk)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)


def _extract_archive(archive, dest_dir):
    if archive.endswith(".tar.gz"):
        import tarfile
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest_dir)
    else:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)


def _locate_executable(tool_dir, binary):
    """解压后定位真正的可执行文件(排除清单/说明/指纹库等杂项)。"""
    candidates = []
    for root, dirs, files in os.walk(tool_dir):
        dirs[:] = [d for d in dirs if d not in ("__MACOSX",)]
        for fn in files:
            if fn in (".DS_Store",):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in _IGNORED_EXT and binary.lower() not in fn.lower():
                continue
            candidates.append(os.path.join(root, fn))

    if not candidates:
        return None

    os_name, arch = detect_platform()
    tokens = _OS_TOKENS[os_name] + _ARCH_TOKENS[arch] + (binary.lower(),)
    for cand in sorted(candidates):
        low = os.path.basename(cand).lower()
        if any(t in low for t in tokens):
            return cand
    return candidates[0]


def is_installed(name, tools_dir):
    """检查工具是否已安装,返回 (present, version, bin_path)。

    PATH 型工具(如 whatweb)不落到 tools/ 目录:通过 shutil.which 检测系统 PATH,
    故 tools_dir 参数对其无效。
    """
    d = TOOLS.get(name)
    if d is None:
        return False, None, os.path.join(tools_dir, name)
    if is_path_tool(name):
        exe, ver = locate_path_tool(name, tools_dir)
        if exe:
            return True, ver, exe
        return False, None, name
    bin_path = os.path.join(tools_dir, name, exe_name(d["bin"]))
    if not os.path.exists(bin_path):
        return False, None, bin_path
    manifest = read_manifest(tools_dir)
    info = manifest.get(name) or {}
    return True, info.get("version") or d.get("version"), bin_path


def install(name, tools_dir, force=False, mirror=None):
    """安装工具到最新版本,返回 (可执行文件绝对路径, 版本)。"""
    d = TOOLS.get(name)
    if d is None:
        raise KeyError("未知工具:{name}".format(name=name))

    os_name, arch = detect_platform()
    if is_path_tool(name):
        exe, _ver = locate_path_tool(name, tools_dir)
        if exe:
            raise RuntimeError("外部工具 {label}({name}) 为系统 PATH 工具(位于 {exe}),无需下载安装".format(
                label=d.get("label", name), name=name, exe=exe))
        raise RuntimeError("外部工具 {label}({name}) 为系统 PATH 工具,未在本地目录/资产/PATH 中检测到;请安装或放置到上述位置后重试".format(
            label=d.get("label", name), name=name))
    tool_dir = os.path.join(tools_dir, name)
    target = os.path.join(tool_dir, exe_name(d["bin"]))

    installed, installed_ver, _ = is_installed(name, tools_dir)
    if installed and not force:
        logger.info("工具 %s 已安装(版本 %s): %s", name, installed_ver, target)
        return target, installed_ver

    version, assets = latest_release(d["repo"], mirror=mirror)
    asset = match_asset(assets, os_name, arch)
    if asset is None:
        raise RuntimeError(
            "{label}({name}) 没有适用于 {os}/{arch} 的预编译包,"
            "可改选 x64 机器或源码编译".format(label=d.get("label", name), name=name, os=os_name, arch=arch))
    if installed and force:
        try:
            shutil.rmtree(tool_dir)
        except OSError:
            pass

    os.makedirs(tool_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="zsans_tm_")
    try:
        archive_path = os.path.join(work, "asset{}".format(
            ".tar.gz" if asset["name"].endswith(".tar.gz") else ".zip"))
        logger.info("下载 %s(版本 %s) 从 %s ...", name, version, asset["url"])
        _download(_mirrorize(asset["url"], mirror), archive_path)
        _extract_archive(archive_path, tool_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    src = _locate_executable(tool_dir, d["bin"])
    if src is None:
        raise RuntimeError("解压后未找到可执行文件:{name}".format(name=name))
    if os.path.abspath(src) != os.path.abspath(target):
        os.rename(src, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
             | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    manifest = read_manifest(tools_dir)
    manifest[name] = {
        "version": version,
        "path": target,
        "platform": "{os}/{arch}".format(os=os_name, arch=arch),
        "asset": asset["name"],
    }
    write_manifest(manifest, tools_dir)
    logger.info("已安装 %s %s: %s", name, version, target)
    return target, version


def remove(name, tools_dir):
    """卸载工具,返回 True 表示确有卸载。PATH 型工具不在插件目录,视为无需卸载(返回 False)。"""
    if name not in TOOLS:
        raise KeyError("未知工具:{name}".format(name=name))
    if is_path_tool(name):
        return False
    tool_dir = os.path.join(tools_dir, name)
    if os.path.isdir(tool_dir):
        shutil.rmtree(tool_dir)
        manifest = read_manifest(tools_dir)
        manifest.pop(name, None)
        write_manifest(manifest, tools_dir)
        logger.info("已卸载 %s", name)
        return True
    return False


def status(tools_dir, include_network=False):
    """返回 ({name: status_dict}, (os_name, arch))。"""
    os_name, arch = detect_platform()
    result = {}
    for name, d in TOOLS.items():
        present, ver, path = is_installed(name, tools_dir)
        state = {"label": d.get("label", name), "installed": present, "path": path, "version": ver,
                 "source": d.get("source") or "download"}
        if include_network and not present and not is_path_tool(name):
            try:
                latest, _ = latest_release(d["repo"])
                state["latest"] = latest
            except Exception:
                state["latest"] = None
        result[name] = state
    return result, (os_name, arch)