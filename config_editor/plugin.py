"""config_editor — 可视化编辑 breeding-config.yaml 的配置界面。

该插件提供一个 Web UI（webui.html）：直接从当前配置文件（breeding-config.yaml）
递归生成表单（开关 / 数字 / 文本 / 下拉 / 列表等），修改后通过文本级 patch
只更新发生变化的 key，保留注释与其余内容。

- 不需要额外 schema 定义：界面完全由配置文件内容驱动，配置里有什么就显示什么。
- 支持在界面上新增配置项（UI 上的「+ 新增配置项」）。
- 该插件不挂载任何 on_* 钩子，仅提供前端界面。
"""

import logging

logger = logging.getLogger("zsans.plugin.config_editor")

__manifest__ = {
    "name": "config_editor",
    "version": "1.1.0",
    "description": "Web UI to edit breeding-config.yaml (config-driven form, text-level save keeps comments)",
    "author": "Z-Sans Contributors",
    "webui": "webui.html",
}