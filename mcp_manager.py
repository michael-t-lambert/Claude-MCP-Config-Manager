#!/usr/bin/env python3
"""
Claude MCP Config Manager
Manages MCP servers and Claude Desktop Extensions.

mcp_library.json  — source of truth, Claude Desktop never touches it.
claude_desktop_config.json   — generated from library on Apply.
Claude Extensions Settings/<id>.json — {"isEnabled": bool}, written on Apply.
manager_prefs.json — persistent UI preferences (restart checkboxes, etc.).
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
import customtkinter as ctk
from tkinter import messagebox

import mcp_updater

# ─── Paths ────────────────────────────────────────────────────────────────────

import sys as _sys
APP_NAME     = "Claude MCP Config Manager"
APP_ID       = "com.mtlam.claude-mcp-config-manager"
SCRIPT_DIR   = Path(_sys.executable).parent if getattr(_sys, 'frozen', False) else Path(__file__).parent
RESOURCE_DIR = Path(getattr(_sys, "_MEIPASS", SCRIPT_DIR))
ICON_FILE    = RESOURCE_DIR / "assets" / "config-toggles.ico"
LIBRARY_FILE = SCRIPT_DIR / "mcp_library.json"
PREFS_FILE   = SCRIPT_DIR / "manager_prefs.json"
CLAUDE_DIR   = Path.home() / "AppData" / "Roaming" / "Claude"
CLAUDE_CFG   = CLAUDE_DIR / "claude_desktop_config.json"
EXT_DIR      = CLAUDE_DIR / "Claude Extensions"
EXT_INSTALL  = CLAUDE_DIR / "extensions-installations.json"
EXT_SETTINGS = CLAUDE_DIR / "Claude Extensions Settings"


# Windows Store app ID — stable across version updates
CLAUDE_DESKTOP_APP_ID = "Claude_pzs8sxrjxfjjc!Claude"

IMPORT_SOURCES = [
    (Path.home() / "Downloads" / "claude_desktop_config.bak.json",    "Downloads backup (.bak.json)"),
    (Path.home() / "Downloads" / "claude_desktop_config.bak (1).json","Downloads backup (1)"),
    (Path.home() / "Downloads" / "claude_desktop_config.json",        "Downloads config"),
    (CLAUDE_CFG,                                                        "AppData live config"),
]


# ─── Theme ────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

INDIGO     = "#6366f1"
INDIGO_HOV = "#4f46e5"
TEAL       = "#0d9488"
TEAL_HOV   = "#0f766e"
BG_DARK    = "#0f0f17"
BG_PANEL   = "#13131f"
BG_CARD    = "#1a1a2e"
BG_SEL     = "#1e1e3a"
BORDER     = "#2d2d4e"

# ─── Manager Preferences ─────────────────────────────────────────────────────

DEFAULT_PREFS = {
    "close_claude_on_apply": True,
    "reopen_claude_on_apply": True,
}

def _set_windows_app_id() -> None:
    if _sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

def _timestamped_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    shutil.copy2(path, backup)
    return backup

def load_prefs() -> dict:
    if PREFS_FILE.exists():
        try:
            data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_PREFS, **data}
        except Exception:
            pass
    return dict(DEFAULT_PREFS)

def save_prefs(prefs: dict) -> None:
    _write_json_atomic(PREFS_FILE, prefs)

# ─── MCP Server I/O ──────────────────────────────────────────────────────────

def load_library() -> tuple[dict, dict, str | None]:
    if LIBRARY_FILE.exists():
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            servers    = data.get("servers", {})
            extensions = data.get("extensions", {})
            discovered = _discover_extensions()
            for eid, ext in discovered.items():
                if eid not in extensions:
                    extensions[eid] = ext
                else:
                    preserved_enabled = extensions[eid].get("enabled", ext.get("enabled", True))
                    extensions[eid] = {**ext, "enabled": preserved_enabled}
            new_servers = _discover_live_servers(servers)
            if new_servers:
                servers.update(new_servers)
                save_library(servers, extensions)
                names = ", ".join(new_servers)
                return servers, extensions, f"Imported {len(new_servers)} new server(s) from Claude Desktop: {names}"
            return servers, extensions, None
        except Exception as exc:
            return {}, {}, f"Error reading library: {exc}"

    servers: dict = {}
    for path, label in IMPORT_SOURCES:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for name, cfg in raw.get("mcpServers", {}).items():
            servers[name] = {"enabled": True, **cfg}
        for name, cfg in raw.get("_mcpServers_disabled", {}).items():
            servers[name] = {"enabled": False, **cfg}
        if servers:
            extensions = _discover_extensions()
            return servers, extensions, f"Library created from: {label}"

    return {}, _discover_extensions(), "No existing MCP config found — starting fresh"


def _discover_live_servers(known_servers: dict) -> dict:
    """Return servers present in claude_desktop_config.json but missing from the library."""
    if not CLAUDE_CFG.exists():
        return {}
    try:
        raw = json.loads(CLAUDE_CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    new: dict = {}
    for name, cfg in raw.get("mcpServers", {}).items():
        if name not in known_servers:
            new[name] = {"enabled": True, **cfg}
    return new


def _discover_extensions() -> dict:
    extensions: dict = {}
    if not EXT_DIR.exists():
        return extensions

    installed: dict = {}
    if EXT_INSTALL.exists():
        try:
            data = json.loads(EXT_INSTALL.read_text(encoding="utf-8"))
            installed = data.get("extensions", {})
        except Exception:
            pass

    for folder in sorted(EXT_DIR.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name.endswith(".disabled"):
            continue
        ext_id = folder.name

        is_enabled = True
        settings_file = EXT_SETTINGS / f"{ext_id}.json"
        if settings_file.exists():
            try:
                s = json.loads(settings_file.read_text(encoding="utf-8"))
                is_enabled = s.get("isEnabled", True)
            except Exception:
                pass

        srv_json: dict = {}
        srv_path = folder / "server.json"
        if srv_path.exists():
            try:
                srv_json = json.loads(srv_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        install_record = installed.get(ext_id, {})
        mf = install_record.get("manifest", {})
        name    = mf.get("name") or srv_json.get("name") or ext_id
        desc    = mf.get("description") or srv_json.get("description") or ""
        version = install_record.get("version") or srv_json.get("version") or ""
        mcp_cfg = mf.get("server", {}).get("mcp_config", {})
        cmd_str = mcp_cfg.get("command", "") if mcp_cfg else ""

        extensions[ext_id] = {
            "enabled": is_enabled, "name": name, "version": version,
            "description": desc, "command_hint": cmd_str,
            "folder": str(folder), "installation_record": install_record,
        }

    return extensions


def save_library(servers: dict, extensions: dict) -> None:
    payload = {
        "_note": "MCP Server Library — managed by mcp_manager.py. Claude Desktop never reads this file.",
        "servers": servers, "extensions": extensions,
    }
    _write_json_atomic(LIBRARY_FILE, payload)

# ─── Apply MCP Config ─────────────────────────────────────────────────────────

def apply_mcp_config(servers: dict) -> tuple[bool, str]:
    existing: dict = {}
    if CLAUDE_CFG.exists():
        try:
            existing = json.loads(CLAUDE_CFG.read_text(encoding="utf-8"))
        except Exception:
            pass

    enabled_servers = {
        name: {k: v for k, v in srv.items() if k != "enabled"}
        for name, srv in servers.items() if srv.get("enabled", False)
    }
    new_cfg = dict(existing)
    new_cfg["mcpServers"] = enabled_servers
    try:
        _timestamped_backup(CLAUDE_CFG)
        _write_json_atomic(CLAUDE_CFG, new_cfg)
        return True, f"{len(enabled_servers)} of {len(servers)} MCP servers active"
    except Exception as exc:
        return False, f"Failed to write MCP config: {exc}"

# ─── Apply Extensions ─────────────────────────────────────────────────────────

def apply_extensions(extensions: dict) -> tuple[bool, str]:
    if not extensions:
        return True, "0 extensions managed"
    try:
        EXT_SETTINGS.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"Cannot create Extensions Settings dir: {exc}"

    errors: list = []
    for ext_id, ext in extensions.items():
        enabled = ext.get("enabled", False)
        settings_file = EXT_SETTINGS / f"{ext_id}.json"
        try:
            _write_json_atomic(settings_file, {"isEnabled": enabled})
        except Exception as exc:
            errors.append(f"{ext_id}: {exc}")

    enabled_count = sum(1 for e in extensions.values() if e.get("enabled"))
    msg = f"{enabled_count} of {len(extensions)} extension{'s' if len(extensions) != 1 else ''} active"
    if errors:
        return False, "; ".join(errors)
    return True, msg

# ─── Claude Desktop Restart ───────────────────────────────────────────────────

def close_claude() -> tuple[bool, str]:
    try:
        subprocess.run(["taskkill", "/IM", "Claude.exe", "/F", "/T"],
                        capture_output=True, text=True, timeout=15)
        for _ in range(10):
            time.sleep(1)
            check = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process -Name 'Claude' -ErrorAction SilentlyContinue).Count"],
                capture_output=True, text=True, timeout=5,
            )
            remaining = check.stdout.strip()
            if not remaining or remaining == "0":
                return True, "Claude Desktop closed"
        return False, f"Claude still running ({remaining} processes)"
    except Exception as exc:
        return False, f"Failed to close Claude: {exc}"

def open_claude() -> tuple[bool, str]:
    """Reopen Claude Desktop via its Windows Store app ID."""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process 'shell:AppsFolder\\{CLAUDE_DESKTOP_APP_ID}'"],
            close_fds=True,
        )
        return True, "Claude Desktop reopening"
    except Exception as exc:
        return False, f"Failed to reopen Claude: {exc}"

# ─── Main Application ─────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        if ICON_FILE.exists():
            try:
                self.iconbitmap(str(ICON_FILE))
            except Exception:
                pass
        self.geometry("1200x680")
        self.minsize(960, 540)
        self.configure(fg_color=BG_DARK)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.prefs = load_prefs()
        saved_geo = self.prefs.get("window_geometry")
        if saved_geo:
            self._restore_geometry(saved_geo)

        self.servers:       dict       = {}
        self.extensions:    dict       = {}
        self.selected:      str | None = None
        self.selected_kind: str        = "server"
        self.dirty:         bool       = False
        self.busy:          bool       = False

        self.close_var  = ctk.BooleanVar(value=self.prefs.get("close_claude_on_apply", True))
        self.reopen_var = ctk.BooleanVar(value=self.prefs.get("reopen_claude_on_apply", True))

        self._build_ui()
        self.after(120, self._load_data)

    def _build_ui(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=58)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="⚙", font=ctk.CTkFont(size=22),
                     text_color=INDIGO).pack(side="left", padx=(16, 6), pady=14)
        ctk.CTkLabel(hdr, text=APP_NAME,
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="white").pack(side="left")
        self.hdr_status = ctk.CTkLabel(hdr, text="", font=ctk.CTkFont(size=12), text_color="#6b7280")
        self.hdr_status.pack(side="right", padx=16)
        ctk.CTkButton(hdr, text="⟳  Check for Updates", width=170, height=30,
                      font=ctk.CTkFont(size=12), fg_color=TEAL, hover_color=TEAL_HOV,
                      command=self._check_updates).pack(side="right", padx=(0, 8), pady=14)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        left = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=0, width=300)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        lt = ctk.CTkFrame(left, fg_color="transparent")
        lt.pack(fill="x", padx=12, pady=(14, 8))
        ctk.CTkLabel(lt, text="MCP Servers & Extensions",
                     font=ctk.CTkFont(size=13, weight="bold"), text_color="#e5e7eb").pack(side="left")
        ctk.CTkButton(lt, text="+ Add", width=68, height=28, font=ctk.CTkFont(size=12),
                      fg_color=INDIGO, hover_color=INDIGO_HOV, command=self._show_add).pack(side="right")
        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x")
        self.list_frame = ctk.CTkScrollableFrame(left, fg_color="transparent",
            scrollbar_button_color="#374151", scrollbar_button_hover_color="#4b5563")
        self.list_frame.pack(fill="both", expand=True, padx=2, pady=4)

        self.right = ctk.CTkFrame(body, fg_color="transparent")
        self.right.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        self._empty_right()

        bot = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=54)
        bot.pack(fill="x")
        bot.pack_propagate(False)

        self.bot_status = ctk.CTkLabel(bot, text="", font=ctk.CTkFont(size=11),
                                        text_color="#6b7280", anchor="w")
        self.bot_status.pack(side="left", fill="x", expand=True, padx=(16, 8))

        self.apply_btn = ctk.CTkButton(bot, text="Apply & Save Config", width=220, height=38,
            font=ctk.CTkFont(size=13, weight="bold"), fg_color=INDIGO, hover_color=INDIGO_HOV,
            command=self._apply)
        self.apply_btn.pack(side="right", padx=(8, 16), pady=8)

        chk_frame = ctk.CTkFrame(bot, fg_color="transparent")
        chk_frame.pack(side="right", pady=8)
        self.reopen_cb = ctk.CTkCheckBox(chk_frame, text="Reopen", variable=self.reopen_var,
            font=ctk.CTkFont(size=11), text_color="#9ca3af", fg_color=INDIGO, hover_color=INDIGO_HOV,
            border_color="#4b5563", width=20, height=20, command=self._on_pref_changed)
        self.reopen_cb.pack(side="right", padx=(8, 0))
        self.close_cb = ctk.CTkCheckBox(chk_frame, text="Close", variable=self.close_var,
            font=ctk.CTkFont(size=11), text_color="#9ca3af", fg_color=INDIGO, hover_color=INDIGO_HOV,
            border_color="#4b5563", width=20, height=20, command=self._on_pref_changed)
        self.close_cb.pack(side="right")

    def _load_data(self) -> None:
        servers, extensions, msg = load_library()
        self.servers    = servers
        self.extensions = extensions
        if msg is None:
            s_active = sum(1 for s in servers.values() if s.get("enabled"))
            e_active = sum(1 for e in extensions.values() if e.get("enabled"))
            self._status(f"Loaded {len(servers)} servers ({s_active} active)  ·  "
                         f"{len(extensions)} extension{'s' if len(extensions) != 1 else ''} ({e_active} active)", "ok")
        elif msg.startswith("Error"):
            self._status(msg, "err")
        elif "fresh" in msg:
            self._status(msg, "warn")
        else:
            save_library(self.servers, self.extensions)
            self._status(msg, "ok")
        self._refresh_list()

    def _refresh_list(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()

        if self.servers:
            names = sorted(self.servers, key=lambda n: (0 if self.servers[n].get("enabled") else 1, n.lower()))
            prev_state = None
            for name in names:
                enabled = self.servers[name].get("enabled", False)
                state = "enabled" if enabled else "disabled"
                if state != prev_state:
                    self._section_label(
                        "  MCP SERVERS — ENABLED" if enabled else "  MCP SERVERS — DISABLED",
                        "#22c55e" if enabled else "#9ca3af",
                        top_pad=2 if prev_state is None else 6)
                    prev_state = state
                self._build_row(name, "server")
        else:
            self._section_label("  MCP SERVERS", "#9ca3af", top_pad=2)
            ctk.CTkLabel(self.list_frame, text="No servers yet. Click + Add.",
                         text_color="#4b5563", font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=4)

        if self.extensions:
            ext_names = sorted(self.extensions,
                key=lambda n: (0 if self.extensions[n].get("enabled") else 1, self.extensions[n].get("name", n).lower()))
            prev_state = None
            for ext_id in ext_names:
                enabled = self.extensions[ext_id].get("enabled", False)
                state = "enabled" if enabled else "disabled"
                if state != prev_state:
                    self._section_label(
                        "  EXTENSIONS — ENABLED" if enabled else "  EXTENSIONS — DISABLED",
                        "#38bdf8" if enabled else "#9ca3af", top_pad=8)
                    prev_state = state
                self._build_row(ext_id, "extension")
        else:
            self._section_label("  EXTENSIONS", "#9ca3af", top_pad=8)
            ctk.CTkLabel(self.list_frame, text="No extensions found.",
                         text_color="#4b5563", font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=4)

    def _section_label(self, text: str, colour: str, top_pad: int = 4) -> None:
        ctk.CTkLabel(self.list_frame, text=text, font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=colour, anchor="w").pack(fill="x", padx=8, pady=(top_pad, 1))

    def _build_row(self, key: str, kind: str) -> None:
        is_ext  = kind == "extension"
        data    = self.extensions[key] if is_ext else self.servers[key]
        enabled = data.get("enabled", False)
        selected = (key == self.selected and kind == self.selected_kind)

        row = ctk.CTkFrame(self.list_frame, fg_color=BG_SEL if selected else "transparent",
                           corner_radius=8, cursor="hand2")
        row.pack(fill="x", padx=4, pady=1)

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=3)

        var = ctk.BooleanVar(value=enabled)
        sw = ctk.CTkSwitch(inner, text="", variable=var, width=38, height=20,
            progress_color=TEAL if is_ext else INDIGO,
            command=lambda k=key, kd=kind, v=var: self._toggle(k, kd, v.get()))
        sw.pack(side="left")

        tf = ctk.CTkFrame(inner, fg_color="transparent")
        tf.pack(side="left", fill="x", expand=True, padx=(10, 0))

        display_name = data.get("name", key) if is_ext else key
        ctk.CTkLabel(tf, text=display_name, anchor="w",
            font=ctk.CTkFont(size=13, weight="bold" if enabled else "normal"),
            text_color="#f3f4f6" if enabled else "#c9cdd3").pack(fill="x")

        # Bind click to all descendants EXCEPT the switch and its children
        def _bind_recursive(widget, callback, skip_widget=None):
            if widget is skip_widget:
                return  # skip this widget AND all its descendants
            widget.bind("<Button-1>", callback)
            for child in widget.winfo_children():
                _bind_recursive(child, callback, skip_widget)

        _bind_recursive(row, lambda e, k=key, kd=kind: self._select(k, kd), skip_widget=sw)

    # ── Right Panel ───────────────────────────────────────────────────────────

    def _empty_right(self) -> None:
        for w in self.right.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.right, text="<-- Select an item to view details",
                     text_color="#4b5563", font=ctk.CTkFont(size=15)).pack(expand=True)

    def _show_detail(self, key: str, kind: str) -> None:
        for w in self.right.winfo_children():
            w.destroy()
        if kind == "extension":
            self._show_ext_detail(key)
        else:
            self._show_server_detail(key)

    def _show_server_detail(self, name: str) -> None:
        srv = self.servers[name]
        enabled = srv.get("enabled", False)

        tr = ctk.CTkFrame(self.right, fg_color="transparent")
        tr.pack(fill="x", pady=(0, 16))
        ctk.CTkLabel(tr, text=name, font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="white").pack(side="left")
        self._badge(tr, enabled)

        card = ctk.CTkFrame(self.right, fg_color=BG_CARD, corner_radius=12)
        card.pack(fill="x", pady=(0, 16))
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()
        self._detail_row(card, "command", srv.get("command", ""))
        args = srv.get("args", [])
        self._detail_row(card, "args", " ".join(str(a) for a in args) if args else "")
        env = srv.get("env", {})
        self._detail_row(card, "env",
            f"{', '.join(env.keys())}  ({len(env)} var{'s' if len(env) != 1 else ''})" if env else "")
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()

        br = ctk.CTkFrame(self.right, fg_color="transparent")
        br.pack(fill="x")
        self._ghost_btn(br, "Edit JSON", lambda: self._show_edit(name))
        self._ghost_btn(br, "Delete", lambda: self._delete_server(name), danger=True)

    def _show_ext_detail(self, ext_id: str) -> None:
        ext = self.extensions[ext_id]
        enabled = ext.get("enabled", False)

        tr = ctk.CTkFrame(self.right, fg_color="transparent")
        tr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(tr, text=ext.get("name", ext_id), font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="white").pack(side="left")
        self._badge(tr, enabled, colour_on="#0e7490")
        ver = ext.get("version", "")
        if ver:
            ctk.CTkLabel(tr, text=f"v{ver}", font=ctk.CTkFont(size=11),
                         text_color="#6b7280").pack(side="left", padx=(10, 0))

        desc = ext.get("description", "")
        if desc:
            ctk.CTkLabel(self.right, text=desc, font=ctk.CTkFont(size=12), text_color="#9ca3af",
                         wraplength=560, anchor="w", justify="left").pack(fill="x", pady=(4, 12))

        card = ctk.CTkFrame(self.right, fg_color=BG_CARD, corner_radius=12)
        card.pack(fill="x", pady=(0, 16))
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()
        self._detail_row(card, "id", ext_id)
        self._detail_row(card, "command", ext.get("command_hint", "") or "managed by extension runtime")
        self._detail_row(card, "folder", ext.get("folder", ""))
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()

        ctk.CTkLabel(self.right,
            text="Uses Claude Desktop's native enable/disable mechanism.\n"
                 "Apply writes  Claude Extensions Settings/<id>.json = {\"isEnabled\": bool}.\n"
                 "Restart Claude Desktop for changes to take effect.",
            font=ctk.CTkFont(size=11), text_color="#4b5563", wraplength=560, anchor="w", justify="left").pack(fill="x")

    def _badge(self, parent, enabled: bool, colour_on: str = "#14532d") -> None:
        ctk.CTkLabel(parent, text=f"  {'enabled' if enabled else 'disabled'}  ",
            font=ctk.CTkFont(size=11), fg_color=colour_on if enabled else "#292929",
            text_color="#86efac" if enabled else "#6b7280", corner_radius=6).pack(side="left", padx=(10, 0))

    def _detail_row(self, parent, label: str, value: str) -> None:
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=18, pady=5)
        ctk.CTkLabel(r, text=label, width=80, anchor="w", font=ctk.CTkFont(size=12),
                     text_color="#6b7280").pack(side="left")
        ctk.CTkLabel(r, text=value or "(none)", anchor="w",
                     font=ctk.CTkFont(size=12, family="Consolas"),
                     text_color="#a78bfa", wraplength=530).pack(side="left", fill="x")

    def _ghost_btn(self, parent, text: str, cmd, danger: bool = False) -> None:
        ctk.CTkButton(parent, text=text, width=108, height=32, font=ctk.CTkFont(size=12),
            fg_color=BG_CARD, hover_color="#7f1d1d" if danger else "#1f2937",
            border_color="#4b5563", border_width=1,
            text_color="#f87171" if danger else "#d1d5db", command=cmd).pack(side="left", padx=(0, 8))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _select(self, key: str, kind: str) -> None:
        self.selected      = key
        self.selected_kind = kind
        self._refresh_list()
        self._show_detail(key, kind)

    def _toggle(self, key: str, kind: str, enabled: bool) -> None:
        if kind == "extension":
            self.extensions[key]["enabled"] = enabled
        else:
            self.servers[key]["enabled"] = enabled
        self.dirty = True
        s_active = sum(1 for s in self.servers.values() if s.get("enabled"))
        e_active = sum(1 for e in self.extensions.values() if e.get("enabled"))
        self._status(f"{s_active}/{len(self.servers)} servers  ·  "
                     f"{e_active}/{len(self.extensions)} extensions active  ·  Unsaved changes", "warn")
        self._refresh_list()
        if self.selected == key and self.selected_kind == kind:
            self._show_detail(key, kind)

    def _delete_server(self, name: str) -> None:
        if not messagebox.askyesno("Delete Server",
            f'Remove "{name}" from the library?\n\nThis cannot be undone.', icon="warning"):
            return
        del self.servers[name]
        self.selected = None
        self.dirty = True
        self._refresh_list()
        self._empty_right()
        self._status(f'Deleted "{name}"  ·  Unsaved changes', "warn")

    def _show_add(self) -> None:
        AddDialog(self, self.servers, self._on_added)

    def _on_added(self, name: str, cfg: dict) -> None:
        self.servers[name] = {"enabled": True, **cfg}
        self.dirty = True
        self.selected = name
        self.selected_kind = "server"
        self._refresh_list()
        self._show_detail(name, "server")
        self._status(f'Added "{name}"  ·  Unsaved changes', "warn")

    def _show_edit(self, name: str) -> None:
        cfg = {k: v for k, v in self.servers[name].items() if k != "enabled"}
        EditDialog(self, name, cfg, lambda new: self._on_edited(name, new))

    def _on_edited(self, name: str, new_cfg: dict) -> None:
        kept = {"enabled": self.servers[name].get("enabled", False)}
        self.servers[name] = {**kept, **new_cfg}
        self.dirty = True
        self._show_detail(name, "server")
        self._status(f'Updated "{name}"  ·  Unsaved changes', "warn")

    def _on_pref_changed(self) -> None:
        self.prefs["close_claude_on_apply"]  = self.close_var.get()
        self.prefs["reopen_claude_on_apply"] = self.reopen_var.get()
        save_prefs(self.prefs)

    def _check_updates(self) -> None:
        # scan reflects the current (possibly unsaved) library the user is looking at
        UpdatesDialog(self, dict(self.servers), dict(self.extensions))

    def _apply(self) -> None:
        if self.busy:
            return
        save_library(self.servers, self.extensions)

        results: list[tuple[bool, str]] = []
        results.append(apply_mcp_config(self.servers))
        results.append(apply_extensions(self.extensions))

        ok = all(result[0] for result in results)
        target_msg = "  ·  ".join(result[1] for result in results)

        if not ok:
            msg = f"✗  {target_msg}".strip()
            self.dirty = True
            self._status(msg, "err")
            messagebox.showerror("Apply Failed", msg)
            return

        self.dirty = False
        do_close  = self.close_var.get()
        do_reopen = self.reopen_var.get()
        config_msg = f"✓  {target_msg}"

        if do_close or do_reopen:
            self._start_restart(config_msg, do_close, do_reopen)
            return

        config_msg += ".  Restart Claude Desktop to apply."
        self._status(config_msg, "ok")

    def _start_restart(self, config_msg: str, do_close: bool, do_reopen: bool) -> None:
        self.busy = True
        self.apply_btn.configure(state="disabled")
        action = "Closing Claude Desktop..." if do_close else "Reopening Claude Desktop..."
        self._status(f"{config_msg}  ·  {action}", "ok")

        def worker() -> None:
            restart_msgs: list[str] = []
            level = "ok"
            if do_close:
                close_ok, close_msg = close_claude()
                restart_msgs.append(close_msg)
                if not close_ok:
                    level = "warn"
                    self.after(0, lambda: self._finish_restart(config_msg, restart_msgs, level))
                    return
            if do_reopen:
                time.sleep(1)
                open_ok, open_msg = open_claude()
                restart_msgs.append(open_msg)
                if not open_ok:
                    level = "warn"
            self.after(0, lambda: self._finish_restart(config_msg, restart_msgs, level))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_restart(self, config_msg: str, restart_msgs: list[str], level: str) -> None:
        self.busy = False
        self.apply_btn.configure(state="normal")
        if restart_msgs:
            config_msg += "  ·  " + "  ·  ".join(restart_msgs)
        self._status(config_msg, level)

    def _restore_geometry(self, geo_str: str) -> None:
        import re
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo_str)
        if not m:
            return
        w, h, x, y = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(-w + 200, min(x, sw - 200))
        y = max(-h + 100, min(y, sh - 100))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Close while working?",
                "Claude restart work is still running. Close the manager anyway?", icon="warning"):
                return
        if self.dirty:
            if not messagebox.askyesno("Unsaved Changes",
                "You have unsaved changes. Close without applying them?", icon="warning"):
                return
        self.prefs["window_geometry"] = self.geometry()
        save_prefs(self.prefs)
        self.destroy()

    def _status(self, msg: str, level: str = "ok") -> None:
        colours = {"ok": "#9ca3af", "warn": "#f59e0b", "err": "#ef4444"}
        c = colours.get(level, "#9ca3af")
        self.hdr_status.configure(text=msg, text_color=c)
        self.bot_status.configure(text=msg, text_color=c)

# ─── Add Server Dialog ────────────────────────────────────────────────────────

class AddDialog(ctk.CTkToplevel):
    def __init__(self, parent, existing, callback):
        super().__init__(parent)
        self.existing = existing
        self.callback = callback
        self.title("Add MCP Server")
        self.geometry("500x510")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=BG_CARD)
        self.after(60, self.lift)

        ctk.CTkLabel(self, text="Add New MCP Server", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="white").pack(anchor="w", padx=20, pady=(20, 14))
        self.name_e  = self._entry("Server Name *", "e.g. My MCP Server")
        self.cmd_e   = self._entry("Command *", "e.g. python, node, cmd, npx")
        self.args_tb = self._textbox("Arguments  (one per line)")
        self.env_tb  = self._textbox("Environment Variables  (KEY=value, one per line)")

        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=20, pady=(10, 20))
        ctk.CTkButton(br, text="Cancel", width=100, fg_color="#1f2937", hover_color="#374151",
                      command=self.destroy).pack(side="left")
        ctk.CTkButton(br, text="Add Server", width=130, fg_color=INDIGO, hover_color=INDIGO_HOV,
                      command=self._submit).pack(side="right")

    def _entry(self, label, placeholder):
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=12), text_color="#9ca3af").pack(anchor="w", padx=20, pady=(0, 2))
        e = ctk.CTkEntry(self, height=34, placeholder_text=placeholder, font=ctk.CTkFont(size=12),
                          fg_color="#111827", border_color="#374151")
        e.pack(fill="x", padx=20, pady=(0, 10))
        return e

    def _textbox(self, label):
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=12), text_color="#9ca3af").pack(anchor="w", padx=20, pady=(0, 2))
        tb = ctk.CTkTextbox(self, height=70, font=ctk.CTkFont(size=12, family="Consolas"),
                             fg_color="#111827", border_color="#374151", border_width=1)
        tb.pack(fill="x", padx=20, pady=(0, 10))
        return tb

    def _submit(self):
        name = self.name_e.get().strip()
        command = self.cmd_e.get().strip()
        if not name:
            messagebox.showerror("Error", "Server name is required.", parent=self); return
        if not command:
            messagebox.showerror("Error", "Command is required.", parent=self); return
        if name in self.existing:
            messagebox.showerror("Error", f'"{name}" already exists.', parent=self); return

        args = [l.strip() for l in self.args_tb.get("1.0", "end").splitlines() if l.strip()]
        env = {}
        for line in self.env_tb.get("1.0", "end").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip():
                    env[k.strip()] = v.strip()
        cfg = {"command": command, "args": args}
        if env:
            cfg["env"] = env
        self.callback(name, cfg)
        self.destroy()

# ─── Edit JSON Dialog ─────────────────────────────────────────────────────────

class EditDialog(ctk.CTkToplevel):
    def __init__(self, parent, name, cfg, callback):
        super().__init__(parent)
        self.callback = callback
        self.title(f"Edit -- {name}")
        self.geometry("600x540")
        self.grab_set()
        self.configure(fg_color=BG_CARD)
        self.after(60, self.lift)

        ctk.CTkLabel(self, text=f"Edit: {name}", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="white").pack(anchor="w", padx=20, pady=(20, 2))
        ctk.CTkLabel(self, text="Modify the JSON config. The enabled/disabled state is controlled by the main toggle.",
                     font=ctk.CTkFont(size=11), text_color="#6b7280", wraplength=560).pack(anchor="w", padx=20, pady=(0, 10))

        self.editor = ctk.CTkTextbox(self, font=ctk.CTkFont(size=12, family="Consolas"),
                                      fg_color="#0d1117", border_color="#374151", border_width=1)
        self.editor.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        self.editor.insert("1.0", json.dumps(cfg, indent=2))

        self.err_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11), text_color="#ef4444")
        self.err_lbl.pack(anchor="w", padx=20)

        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=20, pady=(6, 20))
        ctk.CTkButton(br, text="Validate", width=90, fg_color="#1f2937", hover_color="#374151",
                      command=self._validate).pack(side="left")
        ctk.CTkButton(br, text="Cancel", width=90, fg_color="#1f2937", hover_color="#374151",
                      command=self.destroy).pack(side="left", padx=(8, 0))
        ctk.CTkButton(br, text="Apply Changes", width=150, fg_color=INDIGO, hover_color=INDIGO_HOV,
                      command=self._apply).pack(side="right")

    def _validate(self):
        try:
            json.loads(self.editor.get("1.0", "end"))
            self.err_lbl.configure(text="Valid JSON", text_color="#22c55e")
            return True
        except json.JSONDecodeError as exc:
            self.err_lbl.configure(text=f"JSON error: {exc}", text_color="#ef4444")
            return False

    def _apply(self):
        if not self._validate():
            return
        self.callback(json.loads(self.editor.get("1.0", "end")))
        self.destroy()

# ─── Updates Dialog ───────────────────────────────────────────────────────────

_STATE_STYLE = {
    mcp_updater.UP_TO_DATE:   ("up to date",   "#14532d", "#86efac"),
    mcp_updater.BEHIND:       ("behind",       "#78350f", "#fbbf24"),
    mcp_updater.FLOATING:     ("floating",     "#0c4a6e", "#7dd3fc"),
    mcp_updater.SELF_MANAGED: ("self-managed", "#292929", "#9ca3af"),
    mcp_updater.ERROR:        ("error",        "#450a0a", "#fca5a5"),
}


class UpdatesDialog(ctk.CTkToplevel):
    def __init__(self, parent, servers: dict, extensions: dict):
        super().__init__(parent)
        self.servers    = servers
        self.extensions = extensions
        self.title("MCP Updates")
        self.geometry("860x620")
        self.minsize(720, 480)
        self.configure(fg_color=BG_DARK)
        self.grab_set()
        self.after(60, self.lift)

        hdr = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="MCP Server Updates", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="white").pack(side="left", padx=16)
        self.rescan_btn = ctk.CTkButton(hdr, text="Rescan", width=90, height=30,
            font=ctk.CTkFont(size=12), fg_color="#1f2937", hover_color="#374151",
            command=self._start_scan)
        self.rescan_btn.pack(side="right", padx=16, pady=11)
        self.summary = ctk.CTkLabel(hdr, text="", font=ctk.CTkFont(size=12), text_color="#9ca3af")
        self.summary.pack(side="right", padx=(0, 8))

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent",
            scrollbar_button_color="#374151", scrollbar_button_hover_color="#4b5563")
        self.body.pack(fill="both", expand=True, padx=10, pady=10)

        self._start_scan()

    def _start_scan(self) -> None:
        self.rescan_btn.configure(state="disabled")
        self.summary.configure(text="scanning…")
        for w in self.body.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.body,
            text="Scanning all configs…\n(git fetch + npm/PyPI/GitHub version checks — may take a moment)",
            font=ctk.CTkFont(size=13), text_color="#6b7280", justify="left").pack(anchor="w", padx=14, pady=18)

        def worker() -> None:
            try:
                refs = mcp_updater.collect_all_servers(self.servers, self.extensions)
                results = [(r, mcp_updater.check_update(r)) for r in refs]
            except Exception as exc:  # noqa: BLE001
                results = None
                err = str(exc)
                self.after(0, lambda: self._scan_failed(err))
                return
            self.after(0, lambda: self._render(results))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_failed(self, err: str) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.body, text=f"Scan failed:\n{err}", text_color="#ef4444",
                     font=ctk.CTkFont(size=12), justify="left").pack(anchor="w", padx=14, pady=18)
        self.rescan_btn.configure(state="normal")
        self.summary.configure(text="error")

    def _render(self, results: list) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        order = {mcp_updater.BEHIND: 0, mcp_updater.ERROR: 1, mcp_updater.FLOATING: 2,
                 mcp_updater.UP_TO_DATE: 3, mcp_updater.SELF_MANAGED: 4}
        results.sort(key=lambda rs: (0 if rs[1].upgradable else 1,
                                     order.get(rs[1].state, 9), rs[0].display.lower()))
        upgradable = 0
        for ref, st in results:
            if st.upgradable:
                upgradable += 1
            self._row(ref, st)
        self.summary.configure(text=f"{len(results)} servers  ·  {upgradable} upgradable")
        self.rescan_btn.configure(state="normal")

    def _row(self, ref, st) -> None:
        card = ctk.CTkFrame(self.body, fg_color=BG_CARD, corner_radius=10)
        card.pack(fill="x", padx=6, pady=4)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(top, text=ref.display, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="#f3f4f6").pack(side="left")
        ctk.CTkLabel(top, text=f"  {ref.kind}  ", font=ctk.CTkFont(size=10),
                     fg_color="#1e1e3a", text_color="#a5b4fc", corner_radius=5).pack(side="left", padx=(8, 0))

        label, bg, fg = _STATE_STYLE.get(st.state, ("?", "#292929", "#9ca3af"))
        ctk.CTkLabel(top, text=f"  {label}  ", font=ctk.CTkFont(size=11),
                     fg_color=bg, text_color=fg, corner_radius=6).pack(side="right")

        if st.upgradable:
            ctk.CTkButton(top, text="Upgrade", width=90, height=28,
                font=ctk.CTkFont(size=12, weight="bold"), fg_color=INDIGO, hover_color=INDIGO_HOV,
                command=lambda r=ref, s=st: self._confirm_upgrade(r, s)).pack(side="right", padx=8)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(fill="x", padx=14, pady=(0, 10))
        ver = f"current  {st.current}      →      latest  {st.latest}"
        ctk.CTkLabel(info, text=ver, font=ctk.CTkFont(size=12, family="Consolas"),
                     text_color="#c9cdd3", anchor="w").pack(fill="x")
        origins = ", ".join(ref.origins)
        meta = f"{origins}"
        if st.detail:
            meta += f"   ·   {st.detail}"
        ctk.CTkLabel(info, text=meta, font=ctk.CTkFont(size=11), text_color="#6b7280",
                     anchor="w", justify="left", wraplength=780).pack(fill="x", pady=(2, 0))

    def _confirm_upgrade(self, ref, st) -> None:
        if ref.kind == mcp_updater.GIT:
            steps = (f"  • Full backup of the repo folder + pip-freeze snapshot\n"
                     f"  • git pull --ff-only  ({st.behind_count} commit(s))\n"
                     f"  • Reinstall requirements.txt into the venv (if present)\n"
                     f"  • Smoke-test the server\n"
                     f"  • Write an upgrade + revert document")
            where = f"Project: {ref.project_dir}"
        else:
            steps = (f"  • pip-freeze snapshot of the venv\n"
                     f"  • pip install -U  ({st.current} → {st.latest})\n"
                     f"  • Smoke-test the server\n"
                     f"  • Write an upgrade + revert document")
            where = f"Interpreter: {ref.interpreter}"
        msg = (f'Upgrade "{ref.display}"?\n\n{where}\n\nThis will:\n{steps}\n\n'
               "A full backup and dependency snapshot are taken BEFORE any change, "
               "so you can revert with one click if the smoke test fails.")
        if messagebox.askyesno("Confirm Upgrade", msg, parent=self):
            UpgradeProgressDialog(self, ref, st, on_done=self._start_scan)


class UpgradeProgressDialog(ctk.CTkToplevel):
    def __init__(self, parent, ref, status, on_done=None):
        super().__init__(parent)
        self.ref     = ref
        self.status  = status
        self.on_done = on_done
        self.result  = None
        self.title(f"Upgrading — {ref.display}")
        self.geometry("720x540")
        self.configure(fg_color=BG_CARD)
        self.grab_set()
        self.after(60, self.lift)
        self.protocol("WM_DELETE_WINDOW", self._close)

        ctk.CTkLabel(self, text=f"Upgrading {ref.display}", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="white").pack(anchor="w", padx=20, pady=(18, 2))
        self.head = ctk.CTkLabel(self, text="Working… do not close.", font=ctk.CTkFont(size=12),
                                 text_color="#9ca3af")
        self.head.pack(anchor="w", padx=20, pady=(0, 8))

        self.log = ctk.CTkTextbox(self, font=ctk.CTkFont(size=11, family="Consolas"),
                                  fg_color="#0d1117", border_color="#374151", border_width=1)
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self.log.configure(state="disabled")

        self.btn_row = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_row.pack(fill="x", padx=20, pady=(0, 18))
        self.close_btn = ctk.CTkButton(self.btn_row, text="Close", width=90, state="disabled",
            fg_color="#1f2937", hover_color="#374151", command=self._close)
        self.close_btn.pack(side="right")
        self.doc_btn = None
        self.revert_btn = None

        self._run()

    def _append(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run(self) -> None:
        def worker() -> None:
            res = mcp_updater.upgrade(
                self.ref, self.status,
                on_progress=lambda m: self.after(0, lambda: self._append(m)))
            self.after(0, lambda: self._finish(res))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, res) -> None:
        self.result = res
        self.close_btn.configure(state="normal")
        if res.ok and res.smoke == "PASS":
            self.head.configure(text=f"✓  Upgrade complete — smoke test PASSED  ({res.old_ref[:10]} → {res.new_ref[:10]})",
                                text_color="#22c55e")
        elif res.ok:
            self.head.configure(text=f"Upgrade applied — smoke test {res.smoke}. Review the log; revert if needed.",
                                text_color="#f59e0b")
        else:
            self.head.configure(text="✗  Upgrade did not complete cleanly. Revert available below.",
                                text_color="#ef4444")

        if res.doc_path:
            self.doc_btn = ctk.CTkButton(self.btn_row, text="Open Document", width=130,
                fg_color="#1f2937", hover_color="#374151",
                command=lambda p=res.doc_path: self._open_doc(p))
            self.doc_btn.pack(side="left")

        if res.revert_record:
            danger = (not res.ok) or res.smoke in ("FAIL", "INCONCLUSIVE")
            self.revert_btn = ctk.CTkButton(self.btn_row, text="Revert", width=110,
                fg_color="#7f1d1d" if danger else "#1f2937",
                hover_color="#991b1b" if danger else "#374151",
                text_color="#fca5a5" if danger else "#d1d5db",
                command=self._revert)
            self.revert_btn.pack(side="right", padx=(0, 8))

    def _open_doc(self, path) -> None:
        try:
            os.startfile(str(path))  # noqa: S606 — Windows, user-initiated
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("Document", f"Saved at:\n{path}\n\n({exc})", parent=self)

    def _revert(self) -> None:
        if not messagebox.askyesno("Confirm Revert",
            f'Roll "{self.ref.display}" back to its pre-upgrade state?', parent=self, icon="warning"):
            return
        self.revert_btn.configure(state="disabled")
        self.close_btn.configure(state="disabled")
        self._append("\n── REVERT ──")

        def worker() -> None:
            ok, log = mcp_updater.revert(
                self.result.revert_record,
                on_progress=lambda m: self.after(0, lambda: self._append(m)))
            self.after(0, lambda: self._revert_done(ok))
        threading.Thread(target=worker, daemon=True).start()

    def _revert_done(self, ok: bool) -> None:
        self.close_btn.configure(state="normal")
        self.head.configure(text="Reverted to pre-upgrade state." if ok else "Revert failed — see log.",
                            text_color="#22c55e" if ok else "#ef4444")

    def _close(self) -> None:
        self.destroy()
        if self.on_done:
            try:
                self.on_done()
            except Exception:  # noqa: BLE001
                pass


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _set_windows_app_id()
    app = App()
    app.mainloop()
