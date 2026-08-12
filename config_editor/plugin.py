"""config_editor — Visual editor for breeding-config.yaml.

This plugin provides a Web UI (webui.html) that directly reads the current
configuration file (breeding-config.yaml) and recursively generates a form
(toggles / numbers / text / dropdowns / lists, etc.). After editing, it applies
changes via text‑level patch, updating only the keys that actually changed while
preserving comments and the rest of the content.

- No extra schema definitions required: the interface is entirely driven by the
  configuration file content – whatever is in the config appears in the UI.
- Supports adding new configuration items directly from the UI
  (via the "+ Add config item" button).
- This plugin does not attach any on_* hooks; it only provides a frontend.
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