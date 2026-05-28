# Claude MCP Config Manager

Small Windows desktop utility for keeping a personal library of MCP server definitions and choosing which ones are active when Claude Desktop starts.

## What It Manages

- `mcp_library.json` is the app's source of truth.
- Claude Desktop reads `claude_desktop_config.json`, not the library file.
- On Apply, enabled MCP servers are written to Claude Desktop's `mcpServers` config.
- Claude Desktop Extensions are discovered from the Claude app data folder and enabled or disabled by writing `Claude Extensions Settings/<extension-id>.json`.

## Main Files

- `mcp_manager.py`: application source.
- `mcp_library.json`: MCP server and extension library.
- `manager_prefs.json`: UI preferences, including close/reopen and Claude output toggles.
- `assets/config-toggles.ico`: bundled multi-resolution Windows icon.
- `assets/config-toggles-hires-source.png`: approved high-resolution source artwork for the icon.
- `assets/config-toggles-hires-transparent.png`: transparent-background version used to generate the icon.
- `Claude MCP Config Manager.spec`: PyInstaller build config.
- `Claude MCP Config Manager.exe`: rebuilt executable copied from `dist`.

## Using The App

1. Launch `Claude MCP Config Manager.exe`.
2. Toggle MCP servers or Claude Desktop Extensions in the left pane.
3. Use `+ Add` to add a simple stdio MCP server.
4. Select a server and use `Edit JSON` for full MCP configuration edits.
5. Leave `Close` and `Reopen` checked if you want the app to restart Claude Desktop after applying.
6. Click `Apply & Save Config`.

## Claude Behavior

When `Claude` is selected, Apply:

- Saves `mcp_library.json`.
- Creates a timestamped backup of the existing Claude config.
- Preserves unknown top-level Claude config keys.
- Replaces only the `mcpServers` section with enabled servers.
- Writes extension enabled states.
- Optionally closes and reopens Claude Desktop.

Claude Desktop must be restarted for MCP server and extension changes to take effect.

## Safety And Backups

- Writes are atomic where practical: config data is written to a temporary file and then moved into place.
- Claude config gets a timestamped backup before being rewritten.
- Closing the app with unsaved changes prompts before exit.
- The app currently stores server definitions exactly as entered in `mcp_library.json`, including any environment values.

## Building

From this directory:

```powershell
pyinstaller --clean --noconfirm "Claude MCP Config Manager.spec"
```

The rebuilt executable is created at:

```text
dist\Claude MCP Config Manager.exe
```

The root-level `Claude MCP Config Manager.exe` can be replaced with the rebuilt file after a successful build.

## Icon Notes

The bundled icon contains these sizes:

```text
16, 20, 24, 30, 32, 40, 48, 64, 96, 128, 256
```

The app also sets a Windows AppUserModelID and runtime window icon to help Windows show the custom taskbar icon consistently.
