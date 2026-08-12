# coding: utf-8
"""English external-tool provisioning core for the `toolbox` plugin.

Downloads prebuilt release binaries from GitHub for a registry of tools,
matching the current OS / CPU architecture, and installs them under a tools
directory with a manifest.json. Platform support:

  * OS:      linux / darwin (macOS) / windows
  * Arch:    amd64 / arm64 / 386 / arm

Bundled tools (small, engine-agnostic defaults):

  * subfinder  — subdomain enumeration (projectdiscovery)
  * naabu      — port scanning (projectdiscovery)
  * ehole      — web fingerprinting (EdgeSecurityTeam, keeps extra fingerprint DB)

Other tools can be added at runtime via :func:`register` (the toolbox plugin
exposes this through its ``extra_tools`` config option), so the manager can
handle any GitHub-released CLI binary.
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

logger = logging.getLogger("zsans.plugin.toolbox.provision")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOLS = {
    "subfinder": {
        "repo": "projectdiscovery/subfinder",
        "bin": "subfinder",
        "label": "Subdomain enumeration",
    },
    "naabu": {
        "repo": "projectdiscovery/naabu",
        "bin": "naabu",
        "label": "Port scanning",
    },
    "ehole": {
        "repo": "EdgeSecurityTeam/EHole",
        "bin": "EHole",
        "label": "Web fingerprinting (EHole)",
        # Keep every extracted file: the archive ships a fingerprint DB next
        # to the executable that the tool reads at runtime.
        "keep_extras": True,
    },
    "whatweb": {
        # PATH type: not downloaded; the engine discovers it via system PATH /
        # configured path. Listing just reports presence + version.
        "bin": "whatweb",
        "label": "Web fingerprinting (whatweb, via system PATH)",
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
_IGNORED_EXT = {".json", ".txt", ".md", ".cfg", ".ini", ".yml", ".yaml", ".sh", ".example", ".sbom", ".sig"}


def register(name, repo=None, binary=None, label=None, **kwargs):
    """Add or override a tool definition.

    ``repo`` is the GitHub "owner/repo" that publishes release archives,
    ``binary`` the canonical executable name (without the .exe suffix).
    Returns True when a new tool was added.
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
    logger.info("registered tool definition: %s (repo=%s)", name, d.get("repo"))
    return not existing


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform():
    """Return (os_name, arch), e.g. ("linux", "amd64")."""
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
    """Append the Windows .exe suffix when required."""
    return binary + (".exe" if sys.platform.startswith("win") else "")


def path_tool_version(exe_path):
    """Run '<exe> --version' and extract the first version token, or None."""
    try:
        import subprocess
        out = subprocess.run([exe_path, "--version"], capture_output=True, text=True, timeout=15)
        text = (out.stdout or out.stderr or "").strip()
        m = re.search(r"\d+(?:\.\d+)+", text)
        return m.group(0) if m else (text.splitlines()[0] if text else None)
    except Exception:
        return None


def is_path_tool(name):
    """Whether this tool is PATH-resolved (system/bin-managed) rather than downloaded."""
    d = TOOLS.get(name) or {}
    return bool(d.get("source") == "path")


def locate_path_tool(name, tools_dir):
    """Locate a PATH-type tool, matching the engine's discovery chain.

    Search order: local dirs (multi-arch tools/<name>/{os}/{arch} then a single
    tools/<name> binary) -> assets/ -> system PATH. ``tools_dir`` (the plugin's
    own download dir) is also searched. Returns (abs_exe_path, version) or
    (None, None).
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
    """Default install directory: <plugin_dir>/tools."""
    return os.path.join(plugin_dir, "tools")


def resolve_tools_dir(plugin_dir, cfg_dir):
    """Resolve the configured install dir: absolute paths pass through,
    relative paths are interpreted against the plugin directory."""
    if not cfg_dir:
        return default_tools_dir(plugin_dir)
    if os.path.isabs(cfg_dir):
        return cfg_dir
    return os.path.join(plugin_dir, cfg_dir)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# GitHub release resolution
# ---------------------------------------------------------------------------

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
    """Query the latest GitHub release; returns (version, [assets])."""
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
    """Pick the release asset matching OS + arch, or None."""
    os_tokens = _OS_TOKENS.get(os_name, ("linux",))
    arch_tokens = _ARCH_TOKENS.get(arch, ("amd64",))
    for a in assets:
        n = a["name"].lower()
        if "checksum" in n or ".sig" in n:
            continue
        if not n.endswith(".zip") and not n.endswith(".tar.gz"):
            continue
        if not any(_has_token(n, t) for t in os_tokens):
            continue
        if not any(_has_token(n, t) for t in arch_tokens):
            continue
        return a
    return None


# ---------------------------------------------------------------------------
# Download / extract
# ---------------------------------------------------------------------------

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
    """Find the real executable inside an extracted archive.

    Documentation/checksum/config files are skipped; everything else is
    considered a candidate and the best platform/filename match wins.
    """
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


# ---------------------------------------------------------------------------
# Status / install / remove
# ---------------------------------------------------------------------------

def is_installed(name, tools_dir):
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
    """Install a tool at its latest version.

    Returns (executable_abs_path, version). Reuses an existing install unless
    ``force`` is True.
    """
    d = TOOLS.get(name)
    if d is None:
        raise KeyError("unknown tool: {name}".format(name=name))

    os_name, arch = detect_platform()
    if is_path_tool(name):
        exe, _ver = locate_path_tool(name, tools_dir)
        if exe:
            raise RuntimeError(
                "{label} ({name}) is a system PATH tool (found at {exe}); "
                "nothing to download".format(label=d.get("label", name), name=name, exe=exe))
        raise RuntimeError(
            "{label} ({name}) is a system PATH tool but was not found under local "
            "dirs/assets/PATH; install it with your package manager instead".format(label=d.get("label", name), name=name))
    tool_dir = os.path.join(tools_dir, name)
    target = os.path.join(tool_dir, exe_name(d["bin"]))

    installed, installed_ver, _ = is_installed(name, tools_dir)
    if installed and not force:
        logger.info("tool %s already installed (v%s): %s", name, installed_ver, target)
        return target, installed_ver

    version, assets = latest_release(d["repo"], mirror=mirror)
    asset = match_asset(assets, os_name, arch)
    if asset is None:
        raise RuntimeError(
            "{label} ({name}) has no prebuilt archive for {os}/{arch} on this release; "
            "consider a different architecture or build from source.".format(
                label=d.get("label", name), name=name, os=os_name, arch=arch))
    if installed and force:
        try:
            shutil.rmtree(tool_dir)
        except OSError:
            pass

    os.makedirs(tool_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="toolbox_")
    try:
        archive_path = os.path.join(work, "asset{}".format(
            ".tar.gz" if asset["name"].endswith(".tar.gz") else ".zip"))
        logger.info("downloading %s (v%s) from %s ...", name, version, asset["url"])
        _download(_mirrorize(asset["url"], mirror), archive_path)
        _extract_archive(archive_path, tool_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    src = _locate_executable(tool_dir, d["bin"])
    if src is None:
        raise RuntimeError("no executable found in the extracted archive for {name}".format(name=name))
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
    logger.info("installed %s v%s: %s", name, version, target)
    return target, version


def remove(name, tools_dir):
    """Remove an installed tool directory; returns True if something was removed.
    PATH-type tools are not managed under the plugin dir, so they count as removed=False."""
    if name not in TOOLS:
        raise KeyError("unknown tool: {name}".format(name=name))
    if is_path_tool(name):
        return False
    tool_dir = os.path.join(tools_dir, name)
    if os.path.isdir(tool_dir):
        shutil.rmtree(tool_dir)
        manifest = read_manifest(tools_dir)
        manifest.pop(name, None)
        write_manifest(manifest, tools_dir)
        logger.info("removed %s", name)
        return True
    return False


def status(tools_dir, include_network=False):
    """Return ({name: status_dict}, (os_name, arch))."""
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