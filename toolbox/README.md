# toolbox

A directory-style Z-Sans plugin that manages the external binaries the scan
engine relies on. It must be loaded as a Z-Sans plugin (`--tools` is wired in
via `register_cli`, and scan hooks are responsible for registering tools), but
the download/install logic is fully self-contained (it ships its own
`provision.py`) — it does not depend on the project core's download logic.

## Usage

Once the plugin is loaded, it adds `--tools` to `main.py`:

```bash
python main.py --tools list                          # list all tools and status
python main.py --tools show <pkg>                    # show details of one tool
python main.py --tools install [pkg...]              # install (default: all)
python main.py --tools update  [pkg...]              # update to latest
python main.py --tools remove  [pkg...]              # remove
python main.py --tools enable                        # auto-provision missing tools at scan start
python main.py --tools disable                       # disable auto-provisioning
```

`auto_install` defaults to off — nothing is downloaded without explicit
permission.

## Scanning integration

`on_scan_started` installs missing tools and registers their binaries with the
engine's tool orchestrator; `on_scan_completed` writes
`tools_environment_report.json` to the current run directory.

## Platform support

Linux / macOS / Windows × amd64 / arm64 / 386 / arm. Release archives are
matched from GitHub by OS + architecture.