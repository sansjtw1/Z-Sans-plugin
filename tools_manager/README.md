# tools_manager 插件

外部工具管理器（目录式、自包含）。从 GitHub Release 下载外部工具预编译二进制，
安装到插件自己的 `tools/` 目录并注册进引擎。需作为 Z-Sans 插件加载运行
（由 `register_cli` / 扫描钩子接入），但**下载安装逻辑完全内置在本插件**
（自带 `provision.py`），不依赖项目核心的下载逻辑。

## 使用

插件启用后通过 `register_cli` 向 `main.py` 注入 `--tools`：

```bash
python main.py --tools list                          # 列出所有外部工具及状态
python main.py --tools show <包名>                   # 查看单个工具详情
python main.py --tools install [包名...]             # 安装工具(缺省全部)
python main.py --tools update  [包名...]             # 升级到最新
python main.py --tools remove  [包名...]             # 卸载
python main.py --tools enable                        # 开启扫描启动时的自动补全
python main.py --tools disable                       # 关闭自动补全
```

支持的工具：
- 下载型：subfinder / naabu / EHole（GitHub Release 下载）
- PATH 型：whatweb（仅检测系统 PATH / 本地目录，不下载不卸载）

扫描启动时按配置对缺失工具自动补全；扫描结束生成《工具环境报告》到本次运行目录。