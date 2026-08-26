# Z-Sans 插件仓库

本仓库是 **Z-Sans 资产繁殖引擎** 的官方插件库，用于集中展示与分发生态插件。

- 插件与主项目解耦，可独立下载；把选中的插件文件放进 Z-Sans 的插件目录
  （默认 `plugins/`，可用 `breeding-config.yaml` 的 `plugins.dir` 修改）即可生效。
- 单文件插件：直接把 `.py` 拷入插件目录。
- 目录插件：把整个目录（含 `plugin.py` / `config.yaml` / `README.md` 等）拷入插件目录。
  引擎启动时自动加载，无需额外注册。

> 安装入口：`python main.py --list-plugins` 查看当前已加载插件，
> `python main.py --plugin-info <名称>` 查看单个插件详情。

---

## 插件总览

| 插件 | 类型 | 语言 | 作用 | 依赖 |
|------|------|------|------|------|
| [config_editor](config_editor/) | 目录插件 | EN | Web 可视化编辑 `breeding-config.yaml`（配置驱动表单，文本级保存，保留注释） | 需 `--web` |
| [domain_whois](domain_whois/) | 目录插件 | EN | 通过免费全球化 RDAP 服务为域名资产补充 WHOIS 注册信息 | 网络 |
| [ip_geo](ip_geo.py) | 单文件 | EN | 通过 ip-api.com 查询 IP 资产的地理位置并写入属性 | 网络 |
| [ip_info_cn](ip_info_cn.py) | 单文件 | 中文 | 通过 ip9.com.cn 查询 IP 归属地，输出中文信息 | 网络 |
| [qqbot](qqbot/) | 目录插件 | 中文 | QQ 官方机器人（nonebot-adapter-qq）：远程启动/终止扫描、获取报告；自动拉起 Web 服务；提供 `--qqbot` | nonebot2 + adapter-qq（Python ≥ 3.10）|
| [resource_collector](resource_collector.py) | 单文件 | EN | 下载扫描发现的文件/JS 资源到本地（run_dir 下） | 网络 |
| [security_headers](security_headers.py) | 单文件 | EN | 探测 URL 资产的 HTTP 安全响应头并评分 | 网络 |
| [toolbox](toolbox/) | 目录插件 | EN | 通用外部工具管理器（subfinder/naabu/EHole 及自定义工具），提供 `--tools` | GitHub 下载 |
| [tools_manager](tools_manager/) | 目录插件 | 中文 | 外部工具管理器（下载预编译二进制注册进引擎），提供 `--tools` | GitHub 下载 |

## 互斥关系

以下插件功能重叠、**不能同时加载**，二者选其一（同时存在时由排序靠前者胜出，
败者自动跳过并记入 `engine.plugin_conflicts`）：

| 插件 A | 插件 B | 原因 |
|--------|--------|------|
| `toolbox` | `tools_manager` | 两者都管理外部工具、都注册 `--tools` 参数 |

如已安装两者，请通过配置禁用其一：

```yaml
plugins:
  dir: plugins
  disabled:
    - tools_manager     # 保留 toolbox，禁用 tools_manager
```

> `ip_geo` 与 `ip_info_cn` 都查询 IP 归属地，但接口、字段、语言不同，
> **不互斥**，可同时启用（会各自写入 `properties["geo"]` 与 `properties["ip9"]`）。

---

## 安装方式

### 方式一：手动下载（推荐）

1. 按上表挑选需要的插件，下载对应文件/目录。
2. 放入 Z-Sans 插件目录（默认 `plugins/`，或 `breeding-config.yaml` 中 `plugins.dir` 指定的目录）。
3. 校验互斥关系（见上方表格），重复/冲突插件在 `--list-plugins` 中可见。
4. 启动：`python main.py --list-plugins` 确认已加载；
   单文件插件直接生效；目录插件需含入口文件（`plugin.py` / `main.py` / `<目录名>.py` / `__init__.py`）。

### 方式二：一次性放全部

把本仓库内容整体合并进 `plugins/`，再在配置里禁用不需要的插件：

```yaml
plugins:
  dir: plugins
  disabled:
    - tools_manager     # 与 toolbox 互斥，按需二选一
```

### 说明

- 插件按需选择性下载，避免无用依赖与外部流量
- 插件只在你**主动启用**后才会调用外部接口或下载文件；
  多数插件的总开关默认 `enabled: true`，如不想立刻生效请先配置关闭。
- 所有调用外部服务的插件都设置了超时并在出错时降级为日志，不会阻塞扫描。

## 参与贡献

欢迎提交新插件或改进现有插件。请在本仓库目录下放置：

- 插件入口（单文件 `.py` 或目录 + 入口文件）
- 目录插件建议附带 `config.yaml`（出厂默认值）与 `README.md`（使用说明）
- `__manifest__`（`name` / `version` / `description` / `author`，可选 `webui` / `schema`）