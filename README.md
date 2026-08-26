# Z-Sans Plugin

This repository is the official plugin repository for the **Z-Sans Asset Breeding Engine**, used for centralized presentation and distribution of ecosystem plugins.

- Plugins are decoupled from the main project and can be downloaded independently; place the selected plugin files into Z-Sans’ plugin directory
  (default `plugins/`, can be changed via `plugins.dir` in `breeding-config.yaml`) to activate them.
- Single‑file plugins: copy the `.py` file directly into the plugin directory.
- Directory plugins: copy the entire directory (containing `plugin.py` / `config.yaml` / `README.md`, etc.) into the plugin directory.
  The engine loads them automatically on startup, no additional registration required.

> Installation entry point: `python main.py --list-plugins` to see currently loaded plugins,
> `python main.py --plugin-info <name>` to view details of a single plugin.

---

## Plugin Overview

| Plugin | Type | Language | Purpose | Dependencies |
|--------|------|----------|---------|--------------|
| [config_editor](config_editor/) | Directory | EN | Web-based visual editor for `breeding-config.yaml` (schema‑driven form, text‑level save, preserves comments) | Requires `--web` |
| [domain_whois](domain_whois/) | Directory | EN | Enriches domain assets with WHOIS registration info via free global RDAP services | Network |
| [ip_geo](ip_geo.py) | Single‑file | EN | Queries IP geographic location via ip-api.com and writes to asset attributes | Network |
| [ip_info_cn](ip_info_cn.py) | Single‑file | zh_CN | Queries IP ownership via ip9.com.cn and outputs Chinese location information | Network |
| [qqbot](qqbot/) | Directory | zh_CN | QQ official bot (nonebot-adapter-qq): remote scan control & report delivery via QQ; auto-starts web service; provides `--qqbot` | nonebot2 + adapter-qq (Python ≥ 3.10) |
| [resource_collector](resource_collector.py) | Single‑file | EN | Downloads discovered files/JS resources locally (under `run_dir`) | Network |
| [security_headers](security_headers.py) | Single‑file | EN | Probes HTTP security response headers of URL assets and scores them | Network |
| [toolbox](toolbox/) | Directory | EN | Generic external tool manager (subfinder/naabu/EHole and custom tools), provides `--tools` | GitHub downloads |
| [tools_manager](tools_manager/) | Directory | zh_CN | External tool manager (downloads pre‑built binaries and registers them into the engine), provides `--tools` | GitHub downloads |

## Mutual Exclusion

The following plugins have overlapping functionality and **cannot be loaded simultaneously**—choose only one of the two (if both are present, the one listed first in load order wins,
the loser is skipped automatically and recorded in `engine.plugin_conflicts`):

| Plugin A | Plugin B | Reason |
|----------|----------|--------|
| `toolbox` | `tools_manager` | Both manage external tools and register the `--tools` parameter |

If both are installed, disable one via configuration:

```yaml
plugins:
  dir: plugins
  disabled:
    - tools_manager     # Keep toolbox, disable tools_manager
```

> `ip_geo` and `ip_info_cn` both query IP ownership but use different services, fields, and languages.
> They are **not mutually exclusive** and can be enabled simultaneously (each writes to `properties["geo"]` and `properties["ip9"]` respectively).

---

## Installation Methods

### Method 1: Manual Download (Recommended)

1. Pick the plugins you need from the table above and download the corresponding files/directories.
2. Place them into the Z-Sans plugin directory (default `plugins/`, or the directory specified by `plugins.dir` in `breeding-config.yaml`).
3. Check the mutual exclusion table (see above) – conflicting/duplicate plugins will be visible in `--list-plugins`.
4. Launch: `python main.py --list-plugins` to confirm they are loaded.
   Single‑file plugins take effect immediately; directory plugins need an entry file (`plugin.py` / `main.py` / `<directory_name>.py` / `__init__.py`).

### Method 2: Drop Everything at Once

Merge the entire contents of this repository into `plugins/`, then disable unwanted plugins in the configuration:

```yaml
plugins:
  dir: plugins
  disabled:
    - tools_manager     # Conflicts with toolbox; choose one as needed
```

### Notes

- Plugins are downloaded selectively to avoid unnecessary dependencies and external traffic.
- Plugins only call external APIs or download files **after you actively enable them**;
  most plugins have `enabled: true` by default—if you do not want them active immediately, configure them off first.
- All plugins that call external services have timeouts set and degrade gracefully to logging on errors, so they will not block scanning.

## Contributing

We welcome new plugin submissions or improvements to existing ones. Please place in this repository directory:

- Plugin entry point (single‑file `.py` or directory + entry file)
- Directory plugins are recommended to include a `config.yaml` (factory defaults) and a `README.md` (usage instructions)
- `__manifest__` (`name` / `version` / `description` / `author`, optional `webui` / `schema`)