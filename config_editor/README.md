**config_editor**

A plugin that provides a visual editor for `breeding-config.yaml` with a Web UI.

## Features
- Start the Z-Sans web server first:

``` Bash
python3 main.py --web
```

- In the Z-Sans Web Console, go to the **Plugins** page, click **open** on the `config_editor` row to launch the configuration editor.
- The interface is **driven directly by the current configuration file content** (no schema required, no definition files to maintain):
  Recursively traverses every key in `breeding-config.yaml` and renders the appropriate control based on the value type —
  toggle (bool), number input (number), text input (string), list editor (list), and nested object groups (object).
- Whatever is in the configuration appears in the UI: fields added via the UI, fields manually edited in the YAML, or fields written by third parties are all displayed and editable.
- Reads existing values to populate the form; on save, **only changed fields are submitted** and applied via text‑level patch,
  updating the corresponding lines in the YAML while **preserving comments and the rest of the content** without affecting other settings.
- Supports **adding new configuration items** directly in the UI (click the “+ Add config item” button inside any group/nested block, then enter the key and type).

## Required Backend Endpoints

- `GET /api/config/json` — retrieves the current configuration dict
- `GET /api/plugins/<name>/webui/` — serves resources (e.g., `webui.html`) from the plugin directory
- `POST /api/config/patch` — saves changes at the text level (`{ "updates": { "a.b": value } }`)

## Notes

- This plugin does not attach any `on_*` scanning hooks; it only provides a frontend interface and does not affect scanning behavior.
- The save target is the configuration file currently loaded by the web service (when running `main.py --web`, it is the root‑directory `breeding-config.yaml`).