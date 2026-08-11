#!/usr/bin/env python3
"""
MCP Update Engine  — used by mcp_manager.py (and runnable standalone for a scan).

Pure logic, no GUI. Scans every MCP server the user has configured across four
sources, classifies each by install type, checks for available updates, and — for
the two revertible types only (local git checkouts and pip-installed modules in a
venv) — performs a conservative guided upgrade with full backup, smoke test, and an
auto-generated upgrade/revert document.

Design rules (see the plan / UPGRADE-NOTES-2026-08-11.md):
  * Report-only by default. Nothing is modified without an explicit upgrade() call.
  * git pulls are --ff-only; a dirty working tree or a non-fast-forward aborts.
  * A full backup + pip-freeze snapshot + old-sha record is written BEFORE any change.
  * Floating (npx/uvx), DXT extensions, binaries and node launchers are never modified.
  * Secrets are never printed, logged, or written — redacted by env-key name everywhere.
  * The smoke test never triggers an automatic revert; revert is always user-invoked.

Standalone:  python mcp_updater.py   → prints a read-only scan report.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ─── Paths (self-contained; mirrors mcp_manager.py constants) ──────────────────

SCRIPT_DIR    = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LIBRARY_FILE  = SCRIPT_DIR / "mcp_library.json"
UPGRADE_DOCS  = SCRIPT_DIR / "mcp-upgrades"
HOME          = Path.home()
CLAUDE_DIR    = HOME / "AppData" / "Roaming" / "Claude"
CLAUDE_CFG    = CLAUDE_DIR / "claude_desktop_config.json"
EXT_INSTALL   = CLAUDE_DIR / "extensions-installations.json"
CLAUDE_CODE   = HOME / ".claude.json"

# subprocess niceties for Windows: no console window flashes
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ─── Secret redaction ──────────────────────────────────────────────────────────

_SECRET_HINTS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "APIKEY", "KEY", "PASS", "CREDENTIAL", "AUTH")
_SECRET_SAFE  = ("PYTHONPATH", "SSH_KEY", "--KEY")  # key *paths*, not secrets — but still mask value if it looks secret


def _is_secret_key(key: str) -> bool:
    k = key.upper()
    if k in ("PYTHONPATH", "PYTHONUTF8", "UV_LINK_MODE", "LOG_LEVEL"):
        return False
    return any(h in k for h in _SECRET_HINTS)


def redact_env(env: dict) -> dict:
    """Return a copy of env with secret values masked, for display/logging."""
    out = {}
    for k, v in (env or {}).items():
        if _is_secret_key(k) and v and v != "PASTE_TOKEN_SECRET_HERE":
            out[k] = "••••••••"
        else:
            out[k] = v
    return out


def _secret_values(env: dict) -> list[str]:
    return [v for k, v in (env or {}).items()
            if _is_secret_key(k) and v and len(str(v)) >= 6 and v != "PASTE_TOKEN_SECRET_HERE"]


def scrub(text: str, env: dict) -> str:
    """Strip any secret values (from env) out of captured process output."""
    if not text:
        return text
    for secret in _secret_values(env):
        text = text.replace(str(secret), "••••••••")
    return text

# ─── Data model ────────────────────────────────────────────────────────────────

# kinds
GIT      = "git"        # local git checkout — upgradable
PIP      = "pip"        # pip-installed module — upgradable only in a venv
NPX      = "npx"        # floating npx -y package — report only
UVX      = "uvx"        # floating uvx package — report only
UV_RUN   = "uv-run"     # local uv project (DXT-style) — report only
DXT      = "dxt"        # Claude Desktop extension — report only
BINARY   = "binary"     # native .exe — self-managed
NODE     = "node"       # local node launcher — self-managed
LOCAL    = "local"      # local python package, no upstream detected — report only
UNKNOWN  = "unknown"

_UPGRADABLE = {GIT, PIP}

# states
UP_TO_DATE   = "up-to-date"
BEHIND       = "behind"
FLOATING     = "floating"
SELF_MANAGED = "self-managed"
ERROR        = "error"


@dataclass
class ServerRef:
    key:         str
    names:       list = field(default_factory=list)
    origins:     list = field(default_factory=list)
    command:     str = ""
    args:        list = field(default_factory=list)
    env:         dict = field(default_factory=dict)
    kind:        str = UNKNOWN
    project_dir: Path | None = None
    interpreter: str | None = None
    is_venv:     bool = False
    package:     str | None = None
    ext_record:  dict | None = None

    @property
    def display(self) -> str:
        return self.names[0] if self.names else self.key

    @property
    def redacted_env(self) -> dict:
        return redact_env(self.env)


@dataclass
class UpdateStatus:
    state:        str = ERROR
    current:      str = "?"
    latest:       str = "?"
    detail:       str = ""
    upgradable:   bool = False
    behind_count: int = 0
    dirty:        bool = False


@dataclass
class UpgradeResult:
    ok:            bool
    smoke:         str = "SKIPPED"   # PASS | FAIL | INCONCLUSIVE | SKIPPED
    doc_path:      Path | None = None
    backup_dir:    Path | None = None
    freeze_file:   Path | None = None
    old_ref:       str = ""
    new_ref:       str = ""
    log:           list = field(default_factory=list)
    revert_record: dict | None = None

# ─── Small subprocess helpers ──────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
         timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _git(dir_: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = _run(["git", "-C", str(dir_), *args], timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _http_json(url: str, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mcp-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted registries)
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

# ─── Inventory ─────────────────────────────────────────────────────────────────

def _read_mcp_servers(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return raw.get("mcpServers", {}) or {}


def collect_all_servers(library_servers: dict | None = None,
                        library_extensions: dict | None = None) -> list[ServerRef]:
    """Unified, de-duplicated inventory across all four sources."""
    if library_servers is None:
        if LIBRARY_FILE.exists():
            try:
                data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
                library_servers    = data.get("servers", {})
                library_extensions = library_extensions or data.get("extensions", {})
            except Exception:  # noqa: BLE001
                library_servers = {}
        else:
            library_servers = {}

    sources: list[tuple[str, dict]] = [
        ("Manager library",  library_servers or {}),
        ("Claude Desktop",   _read_mcp_servers(CLAUDE_CFG)),
        ("Claude Code",      _read_mcp_servers(CLAUDE_CODE)),
    ]

    refs: dict[str, ServerRef] = {}
    for origin, servers in sources:
        for name, cfg in servers.items():
            ref = _classify(name, cfg)
            existing = refs.get(ref.key)
            if existing is None:
                ref.origins = [origin]
                ref.names   = [name]
                refs[ref.key] = ref
            else:
                if origin not in existing.origins:
                    existing.origins.append(origin)
                if name not in existing.names:
                    existing.names.append(name)
                # prefer an entry that carries env (more complete for smoke tests)
                if not existing.env and ref.env:
                    existing.env = ref.env

    # DXT extensions
    ext = library_extensions
    if ext is None:
        ext = _discover_ext_records()
    for eid, meta in (ext or {}).items():
        record = meta.get("installation_record", meta)
        key = f"dxt::{eid}"
        refs[key] = ServerRef(
            key=key, names=[meta.get("name", eid)], origins=["DXT extension"],
            command=meta.get("command_hint", ""), kind=DXT, ext_record=record,
        )

    return sorted(refs.values(), key=lambda r: r.display.lower())


def _discover_ext_records() -> dict:
    if not EXT_INSTALL.exists():
        return {}
    try:
        data = json.loads(EXT_INSTALL.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for eid, rec in data.get("extensions", {}).items():
        mf = rec.get("manifest", {})
        out[eid] = {
            "name": mf.get("name", eid),
            "version": rec.get("version", ""),
            "command_hint": mf.get("server", {}).get("mcp_config", {}).get("command", ""),
            "installation_record": rec,
        }
    return out

# ─── Classification ────────────────────────────────────────────────────────────

def _pyvenv(interpreter: str) -> bool:
    """True if interpreter lives in a venv (has ../pyvenv.cfg or a .venv path)."""
    try:
        p = Path(interpreter)
        if (p.parent.parent / "pyvenv.cfg").exists():
            return True
    except Exception:  # noqa: BLE001
        pass
    return ".venv" in interpreter.lower()


def _git_toplevel(start: Path) -> Path | None:
    rc, out, _ = _git(start, "rev-parse", "--show-toplevel", timeout=15)
    if rc == 0 and out:
        return Path(out)
    return None


def _resolve_project_dir(command: str, args: list, env: dict) -> Path | None:
    # 1. an explicit script path (…\main.py)
    for a in args:
        if isinstance(a, str) and a.lower().endswith(".py") and ("\\" in a or "/" in a):
            return Path(a).parent
    # 2. PYTHONPATH
    pp = (env or {}).get("PYTHONPATH")
    if pp:
        return Path(pp.split(os.pathsep)[0])
    # 3. a venv interpreter: <repo>\.venv\Scripts\python.exe → <repo>
    if command.lower().endswith("python.exe") and ".venv" in command.lower():
        try:
            return Path(command).parents[2]
        except Exception:  # noqa: BLE001
            pass
    return None


def classify(cfg: dict) -> str:
    return _classify("", cfg).kind


def _classify(name: str, cfg: dict) -> ServerRef:
    command = str(cfg.get("command", "") or "")
    args    = list(cfg.get("args", []) or [])
    env     = dict(cfg.get("env", {}) or {})
    c       = command.lower()
    ref     = ServerRef(key="", command=command, args=args, env=env)

    lower_args = [str(a).lower() for a in args]

    # npx (bare, or wrapped in `cmd /c npx …`)
    if c.endswith("npx") or c == "npx" or ("npx" in lower_args):
        ref.kind = NPX
        ref.package = _npx_package(args)
        ref.key = f"npm::{ref.package}"
        return ref

    # uvx  (uvx --with X pkg)
    if c.endswith("uvx.exe") or c == "uvx" or c.endswith("/uvx"):
        ref.kind = UVX
        ref.package = _uvx_package(args)
        ref.key = f"pypi::{ref.package}"
        return ref

    # uv --directory <dir> run …
    if (c == "uv" or c.endswith("uv.exe")) and "run" in lower_args:
        ref.kind = UV_RUN
        d = None
        if "--directory" in lower_args:
            i = lower_args.index("--directory")
            if i + 1 < len(args):
                d = Path(str(args[i + 1]))
        ref.project_dir = d
        ref.key = f"uvrun::{str(d).lower() if d else name.lower()}"
        return ref

    # python interpreters — could be git checkout, pip module, or local package
    if c.endswith("python.exe") or c == "python" or c.endswith("/python") or c.endswith("python3"):
        interp = command
        if command in ("python", "python3"):
            interp = shutil.which(command) or command
        ref.interpreter = interp
        ref.is_venv = _pyvenv(interp)

        proj = _resolve_project_dir(command, args, env)
        top  = _git_toplevel(proj) if proj else None
        if top is not None:
            ref.kind = GIT
            ref.project_dir = top
            ref.key = f"git::{str(top).lower()}"
            return ref

        # pip module:  python -m <module>   (no local repo dir)
        if "-m" in args:
            i = args.index("-m")
            if i + 1 < len(args):
                module = str(args[i + 1])
                if proj is not None:
                    # local package on PYTHONPATH but not a git repo → report-only
                    ref.kind = LOCAL
                    ref.project_dir = proj
                    ref.package = module
                    ref.key = f"local::{str(proj).lower()}"
                    return ref
                ref.kind = PIP
                ref.package = module
                ref.key = f"pip::{module.lower()}"
                return ref
        ref.kind = LOCAL
        ref.key = f"local::{name.lower()}"
        return ref

    # node launcher
    if c == "node" or c.endswith("node.exe"):
        ref.kind = NODE
        script = next((str(a) for a in args if str(a).lower().endswith(".js")), name)
        ref.key = f"node::{script.lower()}"
        return ref

    # native binary
    if c.endswith(".exe"):
        ref.kind = BINARY
        ref.key = f"bin::{c}"
        return ref

    ref.kind = UNKNOWN
    ref.key = f"unknown::{name.lower()}"
    return ref


def _npx_package(args: list) -> str:
    skip = {"/c", "/k", "npx", "-y", "--yes", "cmd"}
    for a in args:
        s = str(a)
        if s.lower() in skip or s.startswith("-"):
            continue
        return s
    return "?"


def _uvx_package(args: list) -> str:
    pkg = "?"
    i = 0
    while i < len(args):
        s = str(args[i])
        if s == "--with":            # skip "--with <spec>"
            i += 2
            continue
        if s.startswith("-"):
            i += 1
            continue
        pkg = s                       # last bare positional wins
        i += 1
    return pkg

# ─── Version checks ────────────────────────────────────────────────────────────

def check_update(ref: ServerRef) -> UpdateStatus:
    try:
        if ref.kind == GIT:
            return _check_git(ref)
        if ref.kind == PIP:
            return _check_pip(ref)
        if ref.kind == NPX:
            return _check_floating_npm(ref)
        if ref.kind == UVX:
            return _check_floating_pypi(ref)
        if ref.kind == UV_RUN:
            return UpdateStatus(SELF_MANAGED, "-", "-", "local uv project; update via its own repo")
        if ref.kind == DXT:
            return _check_dxt(ref)
        if ref.kind == LOCAL:
            return UpdateStatus(SELF_MANAGED, "-", "-", "local package; no upstream detected")
        if ref.kind == BINARY:
            return UpdateStatus(SELF_MANAGED, "-", "-", "native binary; updated by its parent app")
        if ref.kind == NODE:
            return UpdateStatus(SELF_MANAGED, "-", "-", "local node launcher; self-updating")
        return UpdateStatus(SELF_MANAGED, "-", "-", "unrecognized launch type")
    except Exception as exc:  # noqa: BLE001
        return UpdateStatus(ERROR, "?", "?", f"check failed: {exc}")


def _check_git(ref: ServerRef) -> UpdateStatus:
    d = ref.project_dir
    rc, head, _ = _git(d, "rev-parse", "--short", "HEAD", timeout=15)
    if rc != 0:
        return UpdateStatus(ERROR, "?", "?", "not a git repository")
    dirty = bool(_git(d, "status", "--porcelain", timeout=20)[1])
    fetch_rc, _, ferr = _git(d, "fetch", "--quiet", timeout=90)
    if fetch_rc != 0:
        return UpdateStatus(ERROR, head, "?", f"fetch failed: {ferr[:120]}", dirty=dirty)
    # upstream ref
    urc, upstream, _ = _git(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=15)
    if urc != 0:
        upstream = "origin/main" if _git(d, "rev-parse", "--verify", "origin/main")[0] == 0 else "origin/master"
    _, up_short, _ = _git(d, "rev-parse", "--short", upstream, timeout=15)
    behind = _git(d, "rev-list", "--count", f"HEAD..{upstream}", timeout=20)[1] or "0"
    try:
        n = int(behind)
    except ValueError:
        n = 0
    ff = _git(d, "merge-base", "--is-ancestor", "HEAD", upstream, timeout=15)[0] == 0
    if n == 0:
        return UpdateStatus(UP_TO_DATE, head, up_short, f"tracking {upstream}", dirty=dirty)
    detail = f"{n} commit(s) behind {upstream}"
    if dirty:
        detail += "  ·  working tree DIRTY (upgrade blocked)"
    elif not ff:
        detail += "  ·  diverged, not fast-forward (upgrade blocked)"
    return UpdateStatus(
        BEHIND, head, up_short, detail,
        upgradable=(not dirty and ff), behind_count=n, dirty=dirty,
    )


def _pip_dist_and_version(interpreter: str, module: str) -> tuple[str | None, str | None]:
    """Resolve the installed distribution name + version for an importable module."""
    code = (
        "import importlib.metadata as m, json;"
        "pd=m.packages_distributions();"
        f"d=pd.get({module!r}) or pd.get({module.replace('-', '_')!r});"
        "print(json.dumps({'dist': d[0] if d else None,"
        " 'ver': (m.version(d[0]) if d else None)}))"
    )
    try:
        p = _run([interpreter, "-c", code], timeout=30)
        if p.returncode == 0 and p.stdout.strip():
            j = json.loads(p.stdout.strip().splitlines()[-1])
            return j.get("dist"), j.get("ver")
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _check_pip(ref: ServerRef) -> UpdateStatus:
    interp = ref.interpreter or shutil.which("python") or "python"
    dist, current = _pip_dist_and_version(interp, ref.package or "")
    guess = dist or (ref.package or "").replace("_", "-")
    pj = _http_json(f"https://pypi.org/pypi/{guess}/json")
    latest = (pj or {}).get("info", {}).get("version") if pj else None
    if current is None:
        return UpdateStatus(ERROR, "?", latest or "?", f"module '{ref.package}' not importable by {Path(interp).name}")
    if latest is None:
        return UpdateStatus(ERROR, current, "?", f"'{guess}' not found on PyPI")
    if current == latest:
        return UpdateStatus(UP_TO_DATE, current, latest, f"{guess} (in {'venv' if ref.is_venv else 'SYSTEM python'})")
    if not ref.is_venv:
        return UpdateStatus(
            BEHIND, current, latest, upgradable=False,
            detail=f"{guess}: newer available, but installed in SHARED system Python — "
                   "auto-upgrade disabled to avoid dependency conflicts. Isolate in a venv first.",
        )
    return UpdateStatus(BEHIND, current, latest, f"{guess}: {current} → {latest}", upgradable=True)


def _check_floating_npm(ref: ServerRef) -> UpdateStatus:
    npm = shutil.which("npm")
    latest = "?"
    if npm:
        try:
            p = _run([npm, "view", ref.package, "version"], timeout=25)
            if p.returncode == 0:
                latest = p.stdout.strip() or "?"
        except Exception:  # noqa: BLE001
            pass
    return UpdateStatus(
        FLOATING, "latest@launch", latest,
        f"npx '{ref.package}' resolves the newest version every launch — no action needed",
    )


def _check_floating_pypi(ref: ServerRef) -> UpdateStatus:
    pj = _http_json(f"https://pypi.org/pypi/{ref.package}/json")
    latest = (pj or {}).get("info", {}).get("version", "?") if pj else "?"
    return UpdateStatus(
        FLOATING, "latest@launch", latest,
        f"uvx '{ref.package}' resolves the newest version every launch — no action needed",
    )


def _check_dxt(ref: ServerRef) -> UpdateStatus:
    rec = ref.ext_record or {}
    current = rec.get("version", "?")
    source = rec.get("source", "")
    if source != "registry":
        return UpdateStatus(SELF_MANAGED, current, "-",
                            "locally-installed DXT extension; no registry upstream")
    repo = rec.get("manifest", {}).get("repository", {}).get("url", "")
    latest = "?"
    if "github.com" in repo:
        slug = repo.split("github.com/")[-1].strip("/").removesuffix(".git")
        gj = _http_json(f"https://api.github.com/repos/{slug}/releases/latest")
        if gj:
            latest = str(gj.get("tag_name", "?")).lstrip("v")
    detail = "DXT extension — update through Claude Desktop's extension manager"
    state = BEHIND if (latest not in ("?", current) and latest != "-") else SELF_MANAGED
    return UpdateStatus(state, current, latest, detail)

# ─── Guided upgrade (git + venv-pip only) ─────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _pip_freeze(interpreter: str, dest: Path) -> bool:
    try:
        p = _run([interpreter, "-m", "pip", "freeze"], timeout=60)
        if p.returncode == 0:
            dest.write_text(p.stdout, encoding="utf-8")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _smoke_test(ref: ServerRef, log: list, timeout: int = 10) -> str:
    """Launch the server; PASS if it starts cleanly, FAIL on crash/traceback."""
    cmd = [ref.command, *[str(a) for a in ref.args]]
    run_env = {**os.environ, **{k: str(v) for k, v in ref.env.items()}}
    log.append(f"smoke: launching `{ref.command} …` (stdin closed, {timeout}s window)")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=run_env,
            cwd=str(ref.project_dir) if ref.project_dir else None,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        log.append(f"smoke: could not launch: {exc}")
        return "FAIL"

    out = ""
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out = out or ""
        rc = None  # still alive at timeout = started OK, was waiting on stdio

    out = scrub(out or "", ref.env)
    tail = "\n".join(out.strip().splitlines()[-12:])
    log.append("smoke output (tail):\n" + (tail or "(no output)"))
    crashed = any(m in out for m in ("Traceback (most recent call last)",
                                     "ModuleNotFoundError", "ImportError", "SyntaxError"))
    if rc is None:
        return "FAIL" if crashed else "PASS"       # alive past window, no crash
    if rc == 0 and not crashed:
        return "PASS"                              # clean exit on EOF
    if crashed or (rc not in (0, None)):
        return "FAIL"
    return "INCONCLUSIVE"


def upgrade(ref: ServerRef, status: UpdateStatus, on_progress=None) -> UpgradeResult:
    """Conservative, fully-reversible upgrade. Only GIT (venv) and venv-PIP."""
    log: list[str] = []

    def emit(msg: str) -> None:
        log.append(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    if ref.kind == GIT:
        return _upgrade_git(ref, status, emit, log)
    if ref.kind == PIP and ref.is_venv:
        return _upgrade_pip(ref, status, emit, log)
    emit(f"Refusing: '{ref.display}' ({ref.kind}) is not an auto-upgradable type.")
    return UpgradeResult(ok=False, log=log)


def _upgrade_git(ref: ServerRef, status: UpdateStatus, emit, log) -> UpgradeResult:
    d = ref.project_dir
    emit(f"Target repo: {d}")

    if bool(_git(d, "status", "--porcelain", timeout=20)[1]):
        emit("ABORT: working tree has uncommitted changes. Nothing was modified.")
        return UpgradeResult(ok=False, log=log)

    _, old_sha, _ = _git(d, "rev-parse", "HEAD")
    _, branch, _ = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
    emit(f"Current HEAD: {old_sha[:10]} on {branch}")

    ts = _ts()
    UPGRADE_DOCS.mkdir(parents=True, exist_ok=True)

    # full backup (skip heavy/ephemeral dirs; git history is kept for wholesale restore)
    backup = d.with_name(f"{d.name}-backup-{ts}")
    emit(f"Backing up repo → {backup}  (excluding .venv, __pycache__)")
    try:
        shutil.copytree(d, backup, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"))
    except Exception as exc:  # noqa: BLE001
        emit(f"ABORT: backup failed: {exc}")
        return UpgradeResult(ok=False, log=log)

    # pip-freeze snapshot of the server's interpreter
    freeze = UPGRADE_DOCS / f"{_slug(ref.display)}-{ts}.freeze.txt"
    interp = ref.interpreter
    if interp and _pip_freeze(interp, freeze):
        emit(f"Saved dependency snapshot → {freeze}")
    else:
        freeze = None
        emit("Note: could not capture pip freeze (interpreter unknown or pip missing).")

    # apply: fast-forward only
    emit("Running: git pull --ff-only")
    rc, out, err = _git(d, "pull", "--ff-only", timeout=180)
    emit((out or err or "").strip() or "(no output)")
    if rc != 0:
        emit("ABORT: git pull --ff-only failed. Repo left at original commit.")
        doc = _write_doc(ref, ts, old_sha, old_sha, backup, freeze, "N/A — pull failed", log, ok=False)
        return UpgradeResult(ok=False, doc_path=doc, backup_dir=backup, freeze_file=freeze,
                             old_ref=old_sha, log=log)

    _, new_sha, _ = _git(d, "rev-parse", "HEAD")
    emit(f"New HEAD: {new_sha[:10]}")

    # reinstall deps if a venv + requirements.txt exist
    req = d / "requirements.txt"
    if interp and ref.is_venv and req.exists():
        emit("Reinstalling requirements into the venv…")
        try:
            p = _run([interp, "-m", "pip", "install", "-r", str(req)], timeout=600)
            emit(scrub((p.stdout or "")[-800:], ref.env) or "(pip done)")
            if p.returncode != 0:
                emit("WARNING: pip install returned non-zero. See smoke test / revert doc.")
        except Exception as exc:  # noqa: BLE001
            emit(f"WARNING: pip install failed: {exc}")

    smoke = _smoke_test(ref, log)
    emit(f"Smoke test: {smoke}")

    revert_record = {
        "kind": GIT, "dir": str(d), "old_sha": old_sha, "new_sha": new_sha,
        "backup": str(backup), "freeze": str(freeze) if freeze else None,
        "interpreter": interp, "display": ref.display,
    }
    doc = _write_doc(ref, ts, old_sha, new_sha, backup, freeze, smoke, log, ok=True)
    ok = smoke != "FAIL"
    return UpgradeResult(ok=ok, smoke=smoke, doc_path=doc, backup_dir=backup, freeze_file=freeze,
                         old_ref=old_sha, new_ref=new_sha, log=log, revert_record=revert_record)


def _upgrade_pip(ref: ServerRef, status: UpdateStatus, emit, log) -> UpgradeResult:
    interp = ref.interpreter
    ts = _ts()
    UPGRADE_DOCS.mkdir(parents=True, exist_ok=True)
    # _check_pip formats the detail as "<dist>: <old> → <new>"
    dist = (status.detail.split(":")[0].strip() if status.detail else "") or ref.package

    freeze = UPGRADE_DOCS / f"{_slug(ref.display)}-{ts}.freeze.txt"
    if not _pip_freeze(interp, freeze):
        emit("ABORT: could not capture pre-upgrade pip freeze — refusing to proceed without a snapshot.")
        return UpgradeResult(ok=False, log=log)
    emit(f"Saved dependency snapshot → {freeze}")

    emit(f"Running: pip install -U {dist}")
    try:
        p = _run([interp, "-m", "pip", "install", "-U", dist], timeout=600)
        emit(scrub((p.stdout or "")[-800:], ref.env) or "(pip done)")
        if p.returncode != 0:
            emit("ABORT: pip install failed. Environment unchanged (snapshot kept).")
            return UpgradeResult(ok=False, freeze_file=freeze, log=log)
    except Exception as exc:  # noqa: BLE001
        emit(f"ABORT: pip install failed: {exc}")
        return UpgradeResult(ok=False, freeze_file=freeze, log=log)

    smoke = _smoke_test(ref, log)
    emit(f"Smoke test: {smoke}")
    revert_record = {
        "kind": PIP, "interpreter": interp, "freeze": str(freeze),
        "dist": dist, "display": ref.display,
    }
    doc = _write_doc(ref, ts, status.current, status.latest, None, freeze, smoke, log, ok=True)
    return UpgradeResult(ok=(smoke != "FAIL"), smoke=smoke, doc_path=doc, freeze_file=freeze,
                         old_ref=status.current, new_ref=status.latest, log=log,
                         revert_record=revert_record)

# ─── Revert ────────────────────────────────────────────────────────────────────

def revert(record: dict, on_progress=None) -> tuple[bool, list]:
    log: list[str] = []

    def emit(msg: str) -> None:
        log.append(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    if record.get("kind") == GIT:
        d = Path(record["dir"])
        emit(f"git reset --hard {record['old_sha'][:10]}  in {d}")
        rc, out, err = _git(d, "reset", "--hard", record["old_sha"], timeout=120)
        emit((out or err or "").strip())
        if rc != 0:
            emit("Revert FAILED. Restore the backup folder manually (see the upgrade doc).")
            return False, log
        freeze = record.get("freeze")
        interp = record.get("interpreter")
        if freeze and interp and Path(freeze).exists():
            emit("Restoring dependencies from the pre-upgrade snapshot…")
            try:
                p = _run([interp, "-m", "pip", "install", "-r", freeze], timeout=600)
                if p.returncode != 0:
                    emit("WARNING: dependency restore returned non-zero.")
            except Exception as exc:  # noqa: BLE001
                emit(f"WARNING: dependency restore failed: {exc}")
        emit("Revert complete.")
        return True, log

    if record.get("kind") == PIP:
        interp = record["interpreter"]
        freeze = record["freeze"]
        emit(f"Restoring {Path(freeze).name} into the venv…")
        try:
            p = _run([interp, "-m", "pip", "install", "-r", freeze], timeout=600)
            ok = p.returncode == 0
            emit("Revert complete." if ok else "WARNING: pip restore returned non-zero.")
            return ok, log
        except Exception as exc:  # noqa: BLE001
            emit(f"Revert FAILED: {exc}")
            return False, log

    emit("Nothing to revert for this record type.")
    return False, log

# ─── Upgrade / revert document ─────────────────────────────────────────────────

def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name).strip("-").lower() or "server"


def _write_doc(ref: ServerRef, ts: str, old_ref: str, new_ref: str,
               backup: Path | None, freeze: Path | None, smoke: str,
               log: list, ok: bool) -> Path:
    UPGRADE_DOCS.mkdir(parents=True, exist_ok=True)
    path = UPGRADE_DOCS / f"{_slug(ref.display)}-{ts}.md"
    is_git = ref.kind == GIT

    revert_cmds = []
    if is_git and ref.project_dir:
        revert_cmds.append(f'git -C "{ref.project_dir}" reset --hard {old_ref}')
        if freeze and ref.interpreter:
            revert_cmds.append(f'& "{ref.interpreter}" -m pip install -r "{freeze}"')
        if backup:
            revert_cmds.append(f'# — or restore the whole folder from: {backup}')
    elif freeze and ref.interpreter:
        revert_cmds.append(f'& "{ref.interpreter}" -m pip install -r "{freeze}"')

    lines = [
        f"# MCP Upgrade — {ref.display} ({ts})",
        "",
        f"- **Result:** {'OK' if ok else 'FAILED / see log'}   ·   **Smoke test:** {smoke}",
        f"- **Kind:** {ref.kind}",
        f"- **Origins:** {', '.join(ref.origins) or '-'}",
        f"- **Names:** {', '.join(ref.names) or ref.display}",
        f"- **Command:** `{ref.command}`",
        f"- **Args:** `{' '.join(str(a) for a in ref.args)}`",
        f"- **Env keys (values redacted):** {', '.join(ref.redacted_env) or '-'}",
    ]
    if ref.project_dir:
        lines.append(f"- **Project dir:** `{ref.project_dir}`")
    if ref.interpreter:
        lines.append(f"- **Interpreter:** `{ref.interpreter}`  ({'venv' if ref.is_venv else 'system'})")
    lines += [
        "",
        "## Version change",
        f"- **From:** `{old_ref}`",
        f"- **To:**   `{new_ref}`",
        "",
        "## Backup / fallback assets",
        f"- **Full folder backup:** `{backup}`" if backup else "- **Full folder backup:** (none — pip-only upgrade)",
        f"- **Dependency snapshot (pip freeze):** `{freeze}`" if freeze else "- **Dependency snapshot:** (none captured)",
        "",
        "## Revert — exact steps",
        "Run in PowerShell to roll back to the pre-upgrade state:",
        "",
        "```powershell",
        *(revert_cmds or ["# No automated revert available; restore the backup folder listed above."]),
        "```",
        "",
        "The manager's **Revert** button performs these same steps automatically.",
        "",
        "## Full log",
        "```",
        *log,
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

# ─── Standalone scan report ────────────────────────────────────────────────────

def _main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # console may be cp1252
    except Exception:  # noqa: BLE001
        pass
    print("Scanning MCP servers across all configs…\n")
    refs = collect_all_servers()
    rows = []
    for ref in refs:
        st = check_update(ref)
        flag = "  ⬆ UPGRADE" if st.upgradable else ""
        rows.append((ref.display, ref.kind, st.state, st.current, st.latest, st.detail, flag))
        print(f"● {ref.display}  [{ref.kind}]  ({', '.join(ref.origins)})")
        print(f"    {st.state:<12} current={st.current}  latest={st.latest}{flag}")
        if st.detail:
            print(f"    {st.detail}")
        if ref.env:
            print(f"    env: {redact_env(ref.env)}")
        print()
    up = sum(1 for r in rows if r[6])
    print(f"— {len(rows)} servers scanned, {up} auto-upgradable —")


if __name__ == "__main__":
    _main()
