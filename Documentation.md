# Claude MCP Config Manager — Technical Documentation

Developer-facing companion to the [README](README.md). Covers architecture, the data model, and the update-scan / guided-upgrade engine internals. For end-user instructions, see the README.

---

## Components

| File | Role |
|------|------|
| `mcp_manager.py` | The customtkinter GUI + all Claude Desktop I/O (library load/save, Apply, restart, dialogs). Entry point. |
| `mcp_updater.py` | Pure-logic update engine (no GUI imports). Inventory, classification, version checks, guided upgrade, revert. Also runnable standalone. |
| `mcp_library.json` | Source of truth for servers + extensions. Gitignored — holds secrets in plaintext. |
| `manager_prefs.json` | UI prefs (close/reopen toggles, window geometry). |
| `mcp-upgrades/` | Auto-generated per-upgrade documents + `pip freeze` snapshots. Gitignored. |
| `Claude MCP Config Manager.spec` | PyInstaller build. Auto-follows `import mcp_updater`; no edits needed to bundle the engine. |

The only third-party dependency is `customtkinter`. The engine uses stdlib only (`urllib`, `subprocess`, `json`, `dataclasses`).

---

## Data model

`mcp_library.json`:

```jsonc
{
  "servers": {
    "<name>": {
      "enabled": true,                // library-only; stripped on Apply
      "command": "cmd",
      "args": ["/c", "npx", "-y", "..."],
      "env": { "KEY": "value" }       // optional
    }
  },
  "extensions": {
    "<extension-id>": {
      "enabled": false,
      "name": "...", "version": "...", "folder": "...",
      "installation_record": { /* mirror of extensions-installations.json entry */ }
    }
  }
}
```

- `enabled` lives only in the library. On **Apply**, `apply_mcp_config()` writes only enabled servers (minus `enabled`) into `claude_desktop_config.json`'s `mcpServers`, preserving all other top-level keys and writing a timestamped backup first.
- Extension enabled-state is applied by writing `Claude Extensions Settings/<id>.json = {"isEnabled": bool}` (`apply_extensions()`).

### Discovery & prune (`load_library`)

On load the manager merges freshly discovered extensions and any live `claude_desktop_config.json` servers not yet in the library. It then **prunes** any library extension whose on-disk folder is gone (guarded on the Claude extensions dir existing), so extensions uninstalled from Claude Desktop disappear from the list.

### Single-instance guard

`_acquire_single_instance()` creates a named mutex (`Local\<APP_ID>`) at startup. If it already exists, `_focus_existing_window()` brings the running copy forward and the new process exits — preventing two instances from racing on `mcp_library.json`.

---

## Update engine (`mcp_updater.py`)

### Inventory (`collect_all_servers`)

Reads four sources, tags each entry's origin, and de-duplicates by a resolved *upgrade key* (repo dir / package / extension id):

1. Manager library (`mcp_library.json`, or the live in-memory dict passed by the GUI)
2. Claude Desktop `claude_desktop_config.json`
3. Claude Code `~/.claude.json` (top-level `mcpServers`)
4. DXT extensions (`extensions-installations.json`)

### Classification (`_classify`)

| Kind | Detected from | Update source | Upgradable |
|------|---------------|---------------|------------|
| `git` | python interp whose resolved project dir is a git repo (script path / `PYTHONPATH` / `.venv` layout → `git rev-parse --show-toplevel`) | `git fetch` + `HEAD..@{u}` | **yes** |
| `pip` | `python -m <module>`, no repo | installed metadata vs PyPI | **yes (venv only)** |
| `npx` | `npx` or `cmd /c npx …` | `npm view <pkg> version` | no (floating) |
| `uvx` | `uvx …` | PyPI JSON | no (floating) |
| `uv-run` | `uv --directory <d> run` | — | no |
| `dxt` | extension record | GitHub release (registry source) | no |
| `binary` / `node` / `local` | `.exe` / `node …` / local package | — | no |

### Version check (`check_update`)

Returns an `UpdateStatus{state, current, latest, detail, upgradable, behind_count, dirty}` where `state ∈ {up-to-date, behind, floating, self-managed, error}`. All network/subprocess calls are timeout-wrapped and never raise into the UI. Only `git` (clean fast-forward) and `pip` (in a venv) set `upgradable=True`.

### Guided upgrade (`upgrade`)

Only for `git` and venv `pip`. Steps, each logged via an `on_progress` callback:

1. **Guard** — git: abort on a dirty working tree or non-fast-forward. Nothing is modified.
2. **Backup** — copy the repo to `<repo>-backup-<ts>` (excluding `.venv`, `__pycache__`); capture `pip freeze` from the server's interpreter; record the old HEAD sha.
3. **Apply** — `git pull --ff-only`; if a venv + `requirements.txt` exist, `pip install -r requirements.txt`. (pip kind: `pip install -U <dist>` after a mandatory freeze snapshot.)
4. **Smoke test** (`_smoke_test`) — launch `command + args` with stdin closed and a short timeout. `PASS` if it exits 0 or survives the window without a traceback; `FAIL` on crash/traceback; `INCONCLUSIVE` otherwise. **Never auto-reverts.**
5. **Document** (`_write_doc`) — write `mcp-upgrades/<name>-<ts>.md` (on success *and* failure) with old→new refs, backup/snapshot paths, exact PowerShell revert commands, and the full log.

### Revert (`revert`)

`git`: `git reset --hard <old_sha>` then reinstall the snapshotted deps. `pip`: reinstall from the freeze snapshot. Always user-initiated (button or the commands in the generated doc).

### Secret redaction

`redact_env` masks values whose key matches secret patterns (`*TOKEN*`, `*PASSWORD*`, `*SECRET*`, `*API_KEY*`, …); `scrub` strips those values out of captured process output. Applied in the results list, upgrade logs, and generated docs — secrets are never printed or written.

### Standalone

```bash
python mcp_updater.py
```

Prints a read-only scan (classification + status + redacted env) for every configured server. Safe — performs `git fetch` and registry reads only.

---

## Build

```powershell
pyinstaller --clean --noconfirm "Claude MCP Config Manager.spec"
```

Produces `dist\Claude MCP Config Manager.exe`. Copy it over the root-level exe (close the running app first — the running exe locks the file). PyInstaller's import graph includes `mcp_updater` automatically.

---

## Security notes

- `mcp_library.json` stores env secrets (tokens, passwords, API keys) in plaintext. It is gitignored, along with `manager_prefs.json`, `mcp-upgrades/`, and `*.bak.json`. Keep the library out of any synced/shared location.
- The update engine never writes or logs secret values.
- System-Python `pip` modules are reported but never auto-upgraded, to avoid breaking other tools that pin conflicting dependency versions (isolate in a venv first).
