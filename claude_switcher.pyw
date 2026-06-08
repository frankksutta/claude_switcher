import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import os
import json
import glob
import threading
import shutil
import queue

# ── Persistence ────────────────────────────────────────────────────────────────
PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_switcher_prefs.json")
# Account/team profiles live in this external JSON (gitignored) so personal
# account info isn't hardcoded in the published source — see load_profiles().
PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_switcher_profiles.json")

def load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass

# ── Claude permission-settings engine (pure, GUI-free) ──────────────────────────
#
# Confirmation prompts are governed by permissions.defaultMode in settings.json.
# Three scopes, precedence local > project > user; defaultMode OVERRIDES (does not
# merge) across scopes. We only ever read/write permissions.defaultMode and never
# clobber any other key (e.g. an existing permissions.allow list).
#   user   → ~/.claude/settings.json
#   project→ <dir>/.claude/settings.json        (committed)
#   local  → <dir>/.claude/settings.local.json  (gitignored)  ← "this project" target
CONFIRM_MODES = ("acceptEdits", "bypassPermissions")


def claude_settings_paths(project_dir):
    """Resolve the three Claude settings.json paths for a given project dir.

    Returns a dict with keys 'user', 'project', 'local'. 'project'/'local' are
    None when project_dir is empty (no project selected).
    """
    user = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    project = local = None
    proj = (project_dir or "").strip()
    if proj:
        claude_dir = os.path.join(proj, ".claude")
        project = os.path.join(claude_dir, "settings.json")
        local = os.path.join(claude_dir, "settings.local.json")
    return {"user": user, "project": project, "local": local}


def read_default_mode(path):
    """Return permissions.defaultMode from a settings file, or None.

    Safe: a missing path, unreadable file, or invalid JSON all yield None.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return None
    mode = perms.get("defaultMode")
    return mode if isinstance(mode, str) else None


def effective_confirm_state(project_dir):
    """Resolve the winning (mode, scope) for a project dir, honoring precedence.

    Precedence local > project > user. Returns (mode, scope) where scope is one
    of 'local'/'project'/'user', or (None, None) if no defaultMode is set
    anywhere.
    """
    paths = claude_settings_paths(project_dir)
    for scope in ("local", "project", "user"):
        mode = read_default_mode(paths.get(scope))
        if mode:
            return mode, scope
    return None, None


def apply_default_mode(path, mode):
    """Merge permissions.defaultMode = mode into the settings file at `path`.

    Loads existing JSON (or starts {} if missing/invalid), sets ONLY
    permissions.defaultMode, preserves every other key (e.g. an allow list),
    creates the parent dir if needed, and writes back with indent=2. Returns the
    written path.
    """
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
    perms["defaultMode"] = mode
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path

# ── Configuration ──────────────────────────────────────────────────────────────
#
#  Confirmed state (June 2026):
#    ~/.claude          → AWS Bedrock  (authMethod: third_party, apiProvider: bedrock)
#    ~/.claude-team1    → Team seat 1  (run: claude auth login with CLAUDE_CONFIG_DIR set)
#    ~/.claude-team2    → Team seat 2  (run: claude auth login with CLAUDE_CONFIG_DIR set)
#
# Env vars that force the Claude CLI onto a cloud backend (Bedrock/Vertex/direct API)
# instead of the subscription login. Team profiles strip ALL of these so the session
# is a clean team-seat login — otherwise a globally-set ANTHROPIC_MODEL=us.anthropic.…
# (needed for the Bedrock profile) leaks in and the team backend rejects it as an
# "invalid model identifier".
BACKEND_ENV_VARS = [
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
]

# Generic, publishable default. The real per-user accounts live in
# PROFILES_FILE (gitignored); this default is written there on first run / when
# the file is missing or invalid. Copy claude_switcher_profiles.example.json and
# edit it (or edit the generated file) to add your own accounts.
GENERIC_DEFAULT_PROFILES = [
    {
        "label": "Account 1 (Team)",
        "tag": "TEAM-1",
        "color": "#00c8ff",
        "config_dir": "~/.claude-team1",
        "mode": "claude",
        "env_set": {},
        "env_remove": list(BACKEND_ENV_VARS),
    },
    {
        "label": "Account 2 (Team)",
        "tag": "TEAM-2",
        "color": "#b87fff",
        "config_dir": "~/.claude-team2",
        "mode": "claude",
        "env_set": {},
        "env_remove": list(BACKEND_ENV_VARS),
    },
    {
        "label": "AWS Bedrock",
        "tag": "BEDROCK",
        "color": "#ff9f43",
        "config_dir": "~/.claude",
        "mode": "bedrock",
        "env_set": {"CLAUDE_CODE_USE_BEDROCK": "1"},
        "env_remove": [],
    },
]

_PROFILE_KEYS = {"label", "tag", "color", "config_dir", "mode", "env_set", "env_remove"}


def _valid_profile_list(data):
    """True if `data` is a non-empty list of dicts that each have every key."""
    if not isinstance(data, list) or not data:
        return False
    return all(isinstance(p, dict) and _PROFILE_KEYS.issubset(p) for p in data)


def load_profiles(path=PROFILES_FILE):
    """Load account profiles from JSON, expanding ~ in each config_dir.

    On a missing or invalid file, writes GENERIC_DEFAULT_PROFILES to `path` and
    returns those. Never raises — any read error falls back to the generic
    default. config_dir is stored with ~ in JSON and expanded here.
    """
    raw = None
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
    except Exception:
        raw = None
    if not _valid_profile_list(raw):
        raw = [dict(p) for p in GENERIC_DEFAULT_PROFILES]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass
    profiles = []
    for p in raw:
        q = dict(p)
        q["config_dir"] = os.path.expanduser(q.get("config_dir", ""))
        profiles.append(q)
    return profiles


PROFILES = load_profiles()

# ── Claude Desktop (Cowork) ────────────────────────────────────────────────────
#
# The Claude Desktop app is an MSIX package; its AppUserModelID is stable
# (the suffix is Anthropic's publisher hash, identical on every machine).
# Its claude:// URL handler accepts "cowork/new" — a bare "claude://cowork"
# is rejected as an unrecognized path.
COWORK_AUMID = r"shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude"
COWORK_URL = "claude://cowork/new"


def cowork_running():
    """True if the Claude Desktop app is running.

    The desktop app's process is named Claude, same as the npm CLI shim
    (claude.exe), so filter by the MSIX install path under WindowsApps.
    """
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "@(Get-Process Claude -ErrorAction SilentlyContinue | "
             "Where-Object { $_.Path -like '*\\WindowsApps\\Claude_*' }).Count"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return int(out.stdout.strip() or 0) > 0
    except Exception:
        return False


# ── Claude disk-usage monitor ──────────────────────────────────────────────────
#
# The Claude Desktop app (MSIX) keeps its data under the virtualized package
# path, NOT %APPDATA%\Claude. ~96% of the footprint is the VM image:
# vm_bundles\claudevm.bundle\rootfs.vhdx is a ~9.4GB decompressed live disk the
# app itself routinely deletes and re-inflates from the kept ~2.2GB
# rootfs.vhdx.zst (see logs\cowork_vm_node.log "rootfs.vhdx missing"). Deleting
# rootfs.vhdx on demand is therefore safe — it mirrors the app's own rotation
# and reclaims the bulk of the space, with the VM re-built from the cached .zst
# (a local decompress, no download) on next launch. Session state lives in
# sessiondata.vhdx, which we never touch.
CLAUDE_PKG_GLOB = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
)
ROOTFS_REL = os.path.join("vm_bundles", "claudevm.bundle", "rootfs.vhdx")
CACHE_DIRS = ["Cache", "Code Cache", "GPUCache", "IndexedDB"]
DEFAULT_LIMIT_GB = 8


def claude_data_dir():
    """Resolve the Claude Desktop data dir (MSIX-virtualized Roaming\\Claude).

    Returns the directory path, or None if it can't be found.
    """
    for m in glob.glob(CLAUDE_PKG_GLOB):
        if os.path.isdir(m):
            return m
    return None


def dir_size(path):
    """Total logical size in bytes of a file or directory tree.

    Unreadable entries are skipped rather than raising; a missing path is 0.
    """
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def claude_footprint():
    """Total bytes consumed by the Claude Desktop data dir (0 if not found)."""
    return dir_size(claude_data_dir())


def human_size(num_bytes):
    """Human-readable byte size, e.g. '9.4 GB', '85.6 MB'."""
    for unit, factor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num_bytes >= factor:
            return "%.1f %s" % (num_bytes / factor, unit)
    return "%d B" % num_bytes


def cleanup_targets(data_dir):
    """Ordered list of safe-to-delete items under the Claude data dir.

    Each entry is {"label", "path", "kind" ('file'|'dir'), "size" (bytes)}.
    Items that don't exist are skipped. Includes the ~9.4GB rootfs.vhdx live
    disk (the app re-inflates it from the kept .zst) and the small caches.
    Deliberately EXCLUDES rootfs.vhdx.zst, sessiondata.vhdx, kernel files, and
    the claude-code* runtime dirs.
    """
    if not data_dir:
        return []
    targets = []
    rootfs = os.path.join(data_dir, ROOTFS_REL)
    if os.path.isfile(rootfs):
        targets.append({"label": "VM live disk (rootfs.vhdx)", "path": rootfs,
                        "kind": "file", "size": dir_size(rootfs)})
    for name in CACHE_DIRS:
        p = os.path.join(data_dir, name)
        if os.path.isdir(p):
            targets.append({"label": name, "path": p, "kind": "dir",
                            "size": dir_size(p)})
    return targets


def delete_path(path):
    """Delete a file or directory tree; return bytes freed (size before delete).

    Files use os.remove; directories use shutil.rmtree with a PowerShell
    Remove-Item fallback on PermissionError (the OneDrive/WinError-5 pattern).
    """
    size = dir_size(path)
    if os.path.isfile(path):
        os.remove(path)
        return size
    try:
        shutil.rmtree(path)
    except PermissionError:
        ps_path = path.replace("'", "''")
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Remove-Item -LiteralPath '{ps_path}' -Recurse -Force"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if os.path.exists(path):
            raise  # fallback didn't work either — don't report it as freed
    return size


def run_cleanup(targets, log_cb, abort_if_running=True):
    """Delete each target, reporting progress through log_cb(message).

    Returns total bytes recovered. If abort_if_running and Claude Desktop is
    running, deletes NOTHING and returns 0 (logs a quit-first message) — the
    app's files are locked / rewritten while it runs.
    """
    if abort_if_running and cowork_running():
        log_cb("Claude Desktop is running — cleanup aborted.")
        log_cb("Please fully quit Claude Desktop, then try again.")
        return 0
    total = 0
    for t in targets:
        log_cb("Deleting %s (%s)…" % (t["label"], human_size(t.get("size", 0))))
        try:
            freed = delete_path(t["path"])
            total += freed
            log_cb("  recovered %s" % human_size(freed))
        except Exception as exc:
            log_cb("  FAILED: %s" % exc)
    log_cb("")
    log_cb("Total recovered: %s" % human_size(total))
    return total


# ── Helpers ────────────────────────────────────────────────────────────────────

def attach_tooltip(widget, text_provider):
    """Attach a lightweight hover tooltip to a Tkinter widget.

    `text_provider` is a callable returning the tooltip string; it's evaluated
    on every hover so dynamic text (e.g. the current path's basename) stays
    fresh. The tooltip is a borderless Toplevel shown on <Enter> and hidden on
    <Leave> / <ButtonPress>. An empty provider result shows nothing.
    """
    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None:
            return
        try:
            text = text_provider()
        except Exception:
            text = ""
        if not text:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        x = widget.winfo_rootx() + 12
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(tip, text=text, font=("Consolas", 8),
                 bg="#1e2330", fg="#e8eaf0", relief="flat", bd=0,
                 padx=6, pady=3, justify="left").pack()
        state["tip"] = tip

    def hide(_event=None):
        if state["tip"] is not None:
            state["tip"].destroy()
            state["tip"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<ButtonPress>", hide, add="+")


def apply_env(profile):
    for key in profile.get("env_remove", []):
        os.environ.pop(key, None)
    for key, val in profile.get("env_set", {}).items():
        os.environ[key] = val
    if profile["config_dir"]:
        os.environ["CLAUDE_CONFIG_DIR"] = profile["config_dir"]
    else:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)


def detect_active_profile():
    bedrock = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "")
    if bedrock == "1":
        return 2
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    for i, p in enumerate(PROFILES):
        if p["config_dir"] and os.path.normpath(p["config_dir"]) == os.path.normpath(config_dir):
            return i
    return 0


# ── App ────────────────────────────────────────────────────────────────────────

class ClaudeSwitcher(tk.Tk):
    BG      = "#0d0f14"
    PANEL   = "#13161e"
    BORDER  = "#1e2330"
    FG      = "#e8eaf0"
    MUTED   = "#555d72"
    ACCENT2 = "#1e2d1e"
    RECENT_HL = "#1f4a63"  # active recent-dir button background
    FONT_HD = ("Consolas", 11, "bold")
    FONT_SM = ("Consolas", 9)

    def __init__(self):
        super().__init__()
        self.title("Claude Code · Account Switcher")
        self.resizable(False, False)
        self.configure(bg=self.BG)

        self.prefs = load_prefs()
        self.selected = tk.IntVar(value=self.prefs.get("profile", detect_active_profile()))
        self.project_dir = tk.StringVar(value=self.prefs.get("project_dir", ""))
        self.extra_ps = tk.BooleanVar(value=self.prefs.get("extra_powershell", False))
        self.open_explorer = tk.BooleanVar(value=self.prefs.get("open_explorer", False))
        # recent dirs list (up to 8)
        self.recent_dirs = self.prefs.get("recent_dirs", [])
        # disk-usage green limit in GB (edit in prefs JSON to change)
        self.disk_limit_gb = self.prefs.get("disk_limit_gb", DEFAULT_LIMIT_GB)

        self._build_ui()
        self._center()
        self._measure_disk()
        # Re-highlight the matching recent-dir button whenever the path changes.
        self.project_dir.trace_add("write", lambda *_: self._refresh_recent_highlight())
        # Persist state when the window is closed via the X button.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=self.BG, pady=14)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="◈ CLAUDE CODE", font=("Consolas", 14, "bold"),
                 bg=self.BG, fg="#00c8ff").pack(side="left")
        tk.Label(hdr, text="account switcher", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(side="left", padx=(8,0), pady=(4,0))
        help_btn = tk.Button(
            hdr, text="?",
            font=self.FONT_SM,
            bg=self.PANEL, fg=self.MUTED,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0,
            padx=8, pady=2,
            command=self._show_help
        )
        help_btn.pack(side="right")

        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")

        # Profile cards
        body = tk.Frame(self, bg=self.BG, pady=16)
        body.pack(fill="x", padx=16)
        tk.Label(body, text="SELECT PROFILE", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(anchor="w", padx=4, pady=(0,8))

        self.cards = []
        for i, p in enumerate(PROFILES):
            card = self._make_card(body, i, p)
            card.pack(fill="x", pady=3)
            self.cards.append(card)

        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")

        # ── Project directory section ──────────────────────────────────────────
        proj_section = tk.Frame(self, bg=self.BG, pady=12)
        proj_section.pack(fill="x", padx=16)

        tk.Label(proj_section, text="PROJECT DIRECTORY", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=(0,6))

        dir_row = tk.Frame(proj_section, bg=self.BG)
        dir_row.pack(fill="x")

        # Text entry for path
        self.dir_entry = tk.Entry(
            dir_row,
            textvariable=self.project_dir,
            font=self.FONT_SM,
            bg=self.PANEL, fg=self.FG,
            insertbackground=self.FG,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor="#00c8ff",
        )
        self.dir_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0,6))
        attach_tooltip(
            self.dir_entry,
            lambda: (os.path.basename(os.path.normpath(self.project_dir.get().strip()))
                     or "(no folder)") if self.project_dir.get().strip()
            else "(no folder)",
        )

        browse_btn = tk.Button(
            dir_row, text="📁  Browse",
            font=self.FONT_SM,
            bg=self.PANEL, fg=self.FG,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0,
            padx=10, pady=6,
            command=self._browse
        )
        browse_btn.pack(side="left")

        clear_btn = tk.Button(
            dir_row, text="✕",
            font=self.FONT_SM,
            bg=self.PANEL, fg=self.MUTED,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0,
            padx=8, pady=6,
            command=lambda: self.project_dir.set("")
        )
        clear_btn.pack(side="left", padx=(4,0))

        # Recent dirs dropdown
        if self.recent_dirs:
            self._build_recent_menu(proj_section)

        # Reduce-confirmations button
        self.min_confirm_btn = tk.Button(
            proj_section,
            text="🛡  REDUCE CONFIRMATIONS…",
            font=self.FONT_SM,
            bg=self.PANEL, fg=self.FG,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0,
            padx=10, pady=6,
            command=self._open_min_confirm,
        )
        self.min_confirm_btn.pack(anchor="w", pady=(10, 0))
        attach_tooltip(
            self.min_confirm_btn,
            lambda: "Lower Claude Code's confirmation prompts for this project "
                    "or globally.",
        )

        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")

        # Status bar
        status_row = tk.Frame(self, bg=self.BG, pady=10)
        status_row.pack(fill="x", padx=20)
        tk.Label(status_row, text="ACTIVE →", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(side="left")
        self.status_lbl = tk.Label(status_row, text="", font=self.FONT_SM,
                                   bg=self.BG, fg=self.FG)
        self.status_lbl.pack(side="left", padx=(6,0))
        self._refresh_status()

        # Launch button
        btn_frame = tk.Frame(self, bg=self.BG, pady=12)
        btn_frame.pack(fill="x", padx=16)
        extra_cb = tk.Checkbutton(
            btn_frame,
            text="Launch an extra PowerShell in project dir",
            variable=self.extra_ps,
            font=self.FONT_SM,
            bg=self.BG, fg=self.FG,
            activebackground=self.BG, activeforeground=self.FG,
            selectcolor=self.PANEL,
            relief="flat", bd=0, cursor="hand2",
            command=self._save_prefs,
        )
        extra_cb.pack(anchor="w")
        explorer_cb = tk.Checkbutton(
            btn_frame,
            text="Open File Explorer in project dir",
            variable=self.open_explorer,
            font=self.FONT_SM,
            bg=self.BG, fg=self.FG,
            activebackground=self.BG, activeforeground=self.FG,
            selectcolor=self.PANEL,
            relief="flat", bd=0, cursor="hand2",
            command=self._save_prefs,
        )
        explorer_cb.pack(anchor="w", pady=(0, 8))
        self.launch_btn = tk.Button(
            btn_frame,
            text="▶  LAUNCH IN POWERSHELL",
            font=self.FONT_HD,
            bg="#00c8ff", fg="#0d0f14",
            activebackground="#33d4ff", activeforeground="#0d0f14",
            relief="flat", cursor="hand2", bd=0,
            padx=20, pady=10,
            command=self._launch
        )
        self.launch_btn.pack(fill="x")
        self.launch_btn.bind("<Enter>", lambda e: self.launch_btn.config(bg="#33d4ff"))
        self.launch_btn.bind("<Leave>", lambda e: self.launch_btn.config(bg="#00c8ff"))

        self.cowork_btn = tk.Button(
            btn_frame,
            text="⬡  CLAUDE COWORK (DESKTOP APP)",
            font=self.FONT_HD,
            bg="#d97757", fg="#0d0f14",
            activebackground="#e08868", activeforeground="#0d0f14",
            relief="flat", cursor="hand2", bd=0,
            padx=20, pady=8,
            command=self._launch_cowork
        )
        self.cowork_btn.pack(fill="x", pady=(6, 0))
        self.cowork_btn.bind("<Enter>", lambda e: self.cowork_btn.config(bg="#e08868"))
        self.cowork_btn.bind("<Leave>", lambda e: self.cowork_btn.config(bg="#d97757"))

        # ── Disk usage monitor ──────────────────────────────────────────────────
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")
        disk_section = tk.Frame(self, bg=self.BG, pady=12)
        disk_section.pack(fill="x", padx=16)

        head_row = tk.Frame(disk_section, bg=self.BG)
        head_row.pack(fill="x")
        tk.Label(head_row, text="DISK USAGE", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(side="left")
        self.disk_lbl = tk.Label(head_row, text="measuring…", font=self.FONT_SM,
                                 bg=self.BG, fg=self.MUTED)
        self.disk_lbl.pack(side="right")

        self.disk_canvas = tk.Canvas(disk_section, height=14, bg=self.PANEL,
                                     highlightthickness=1,
                                     highlightbackground=self.BORDER, bd=0)
        self.disk_canvas.pack(fill="x", pady=(6, 0))

        # Lights up (red, enabled) only when over the limit; greyed otherwise.
        self.clean_btn = tk.Button(
            disk_section,
            text="🧹  CLEAN UP EXCESS",
            font=self.FONT_HD,
            bg=self.PANEL, fg=self.MUTED,
            activebackground="#ff6b5e", activeforeground="#0d0f14",
            disabledforeground=self.MUTED,
            relief="flat", cursor="arrow", bd=0,
            padx=20, pady=8,
            state="disabled",
            command=self._clean_disk,
        )
        self.clean_btn.pack(fill="x", pady=(8, 0))
        self.clean_btn.bind(
            "<Enter>",
            lambda e: self.clean_btn.config(bg="#ff6b5e")
            if str(self.clean_btn["state"]) == "normal" else None)
        self.clean_btn.bind(
            "<Leave>",
            lambda e: self.clean_btn.config(bg="#f85149")
            if str(self.clean_btn["state"]) == "normal" else None)

        tk.Label(self, text="window closes on launch",
                 font=self.FONT_SM, bg=self.BG, fg=self.MUTED).pack(pady=(0,14))

    def _build_recent_menu(self, parent):
        recent_row = tk.Frame(parent, bg=self.BG)
        recent_row.pack(fill="x", pady=(6,0))

        tk.Label(recent_row, text="Recent:", font=self.FONT_SM,
                 bg=self.BG, fg=self.MUTED).pack(side="left", padx=(0,6))

        # (button, path) pairs so the one matching project_dir can be highlighted.
        self.recent_buttons = []
        # Show up to 4 recent dirs as quick-select buttons
        for d in self.recent_dirs[:4]:
            short = os.path.basename(d) or d
            btn = tk.Button(
                recent_row,
                text=short,
                font=self.FONT_SM,
                bg=self.BORDER, fg=self.FG,
                activebackground="#2a2f3e", activeforeground=self.FG,
                relief="flat", cursor="hand2", bd=0,
                padx=8, pady=3,
                command=lambda path=d: self.project_dir.set(path)
            )
            btn.pack(side="left", padx=(0,4))
            attach_tooltip(btn, lambda path=d: path)
            self.recent_buttons.append((btn, d))
        self._refresh_recent_highlight()

    def _refresh_recent_highlight(self):
        """Highlight the recent-dir button matching the current project_dir.

        Compares with normcase+normpath so casing / slash direction don't matter.
        The matching button gets the accent bg; all others revert to BORDER.
        """
        buttons = getattr(self, "recent_buttons", None)
        if not buttons:
            return
        cur = self.project_dir.get().strip()
        cur_key = os.path.normcase(os.path.normpath(cur)) if cur else None
        for btn, path in buttons:
            path_key = os.path.normcase(os.path.normpath(path))
            btn.config(bg=self.RECENT_HL if cur_key and path_key == cur_key
                       else self.BORDER)

    def _make_card(self, parent, index, profile):
        accent = profile["color"]
        outer = tk.Frame(parent, bg=self.BORDER, padx=1, pady=1)
        inner = tk.Frame(outer, bg=self.PANEL, cursor="hand2")
        inner.pack(fill="both", expand=True)

        stripe = tk.Frame(inner, bg=accent, width=4)
        stripe.pack(side="left", fill="y")

        content = tk.Frame(inner, bg=self.PANEL, padx=12, pady=10)
        content.pack(side="left", fill="both", expand=True)

        rb = tk.Radiobutton(
            content, variable=self.selected, value=index,
            bg=self.PANEL, activebackground=self.PANEL,
            selectcolor=self.PANEL, fg=accent, activeforeground=accent,
            command=self._refresh_status, relief="flat", bd=0
        )
        rb.pack(side="left")

        lbl_frame = tk.Frame(content, bg=self.PANEL)
        lbl_frame.pack(side="left", padx=6)

        tk.Label(lbl_frame, text=profile["label"],
                 font=self.FONT_HD, bg=self.PANEL, fg=self.FG).pack(anchor="w")

        tag_text = f"[{profile['tag']}]"
        if profile["mode"] == "bedrock":
            tag_text += "  CLAUDE_CODE_USE_BEDROCK=1"
        else:
            tag_text += f"  {profile['config_dir']}"

        tk.Label(lbl_frame, text=tag_text,
                 font=self.FONT_SM, bg=self.PANEL, fg=self.MUTED).pack(anchor="w")

        # Bind click on the card and every descendant (labels included) so
        # the whole card is clickable, not just the radio circle.
        def bind_card(widget):
            widget.bind("<Button-1>", lambda e, i=index: self._select(i))
            widget.configure(cursor="hand2")
            for child in widget.winfo_children():
                bind_card(child)
        bind_card(outer)

        return outer

    # ── Logic ─────────────────────────────────────────────────────────────────

    def _show_help(self):
        messagebox.showinfo(
            "About Claude Code Account Switcher",
            "Use multiple Claude accounts SIMULTANEOUSLY — each launch opens "
            "its own shell with its own isolated environment, so sessions on "
            "different accounts run side by side without interfering.\n\n"
            "How it works:\n"
            "• Each profile points the Claude CLI at its own config dir via "
            "CLAUDE_CONFIG_DIR (Team 1 → ~/.claude-team1, Team 2 → "
            "~/.claude-team2, Bedrock → ~/.claude).\n"
            "• Team profiles strip all cloud-backend env vars "
            "(CLAUDE_CODE_USE_BEDROCK, ANTHROPIC_MODEL, …) so the session is "
            "a clean team-seat login.\n"
            "• The Bedrock profile sets CLAUDE_CODE_USE_BEDROCK=1 to use AWS "
            "Bedrock instead of a subscription login.\n\n"
            "Project directory: the new shell starts in this folder (recent "
            "folders appear as quick-select buttons). Leave it empty to start "
            "in the default location.\n\n"
            "Extra PowerShell: when checked, launching also opens a second "
            "plain PowerShell window in the project dir — handy for running "
            "git or scripts next to your Claude session.\n\n"
            "Open File Explorer: when checked, launching also opens a File "
            "Explorer window in the project dir.\n\n"
            "Reduce confirmations: the 🛡 REDUCE CONFIRMATIONS… button opens a "
            "window that lowers Claude Code's permission prompts — either for the "
            "selected project (.claude/settings.local.json) or globally "
            "(~/.claude/settings.json). \"Auto-accept edits\" lets Claude edit "
            "files in the project without asking (it still asks for shell "
            "commands and out-of-project writes); \"Skip all confirmations\" "
            "silences everything (use only in trusted/throwaway setups). It only "
            "ever changes permissions.defaultMode and preserves your other "
            "settings.\n\n"
            "Claude Cowork: launches the Claude Desktop app straight into a "
            "new Cowork session — or, if the app is already running, just "
            "brings its window to the front. (Cowork uses the desktop app's "
            "own login; the profiles above don't apply to it.)\n\n"
            "Disk usage: the bar shows how much disk the Claude Desktop app is "
            "using. It's green under the 8 GB limit and red over it. Most of the "
            "space is the Claude Code VM image. When over the limit, the "
            "CLEAN UP EXCESS button lights up: it deletes the VM's live disk "
            "(rootfs.vhdx, ~9 GB) plus the browser caches, freeing the bulk of "
            "the space. This is the same rotation the app does itself — the VM "
            "is rebuilt from a kept compressed copy on the next launch (so that "
            "first launch is slower), and your sessions are untouched. Claude "
            "Desktop must be fully quit first; the limit is set via "
            "\"disk_limit_gb\" in the prefs file.\n\n"
            "Pick a profile, then LAUNCH. Run the switcher again to open "
            "another account in parallel."
        )

    def _select(self, index):
        self.selected.set(index)
        self._refresh_status()

    def _refresh_status(self):
        idx = self.selected.get()
        p = PROFILES[idx]
        self.status_lbl.config(text=p["label"], fg=p["color"])

    def _measure_disk(self):
        """Measure the Claude footprint off the UI thread, then redraw the bar.

        The worker only writes a plain attribute; all Tkinter calls stay on the
        main thread via _poll_disk (Tkinter is not thread-safe).
        """
        self.disk_lbl.config(text="measuring…", fg=self.MUTED)
        self._disk_result = None

        def worker():
            try:
                self._disk_result = claude_footprint()
            except Exception:
                self._disk_result = -1

        threading.Thread(target=worker, daemon=True).start()
        self._poll_disk()

    def _poll_disk(self):
        """Main-thread poller: redraw the bar once the worker has a result."""
        if self._disk_result is None:
            self.after(100, self._poll_disk)
        else:
            self._update_disk_bar(self._disk_result)

    def _update_disk_bar(self, total_bytes):
        """Redraw the disk bar + label from a measured byte count.

        total_bytes < 0 (or None) signals a measurement error / missing data dir.
        """
        self.disk_canvas.delete("all")
        self.update_idletasks()
        w = self.disk_canvas.winfo_width()
        h = int(self.disk_canvas["height"])

        if total_bytes is None or total_bytes < 0:
            self.disk_lbl.config(text="unavailable", fg=self.MUTED)
            self.clean_btn.config(state="disabled", bg=self.PANEL,
                                  fg=self.MUTED, cursor="arrow")
            return

        limit = self.disk_limit_gb
        gb = total_bytes / 1024 ** 3
        over = gb >= limit
        frac = min(gb / limit, 1.0) if limit else 0
        color = "#f85149" if over else "#3fb950"
        if w > 1 and frac > 0:
            self.disk_canvas.create_rectangle(0, 0, int(w * frac), h,
                                              fill=color, width=0)
        self.disk_lbl.config(text="%.1f GB / %.1f GB" % (gb, limit), fg=color)

        if over:
            self.clean_btn.config(state="normal", bg="#f85149",
                                  fg="#0d0f14", cursor="hand2")
        else:
            self.clean_btn.config(state="disabled", bg=self.PANEL,
                                  fg=self.MUTED, cursor="arrow")

    def _clean_disk(self):
        """Clean up Claude's excess disk usage, gated on Claude Desktop being quit.

        Deletion runs on a worker thread; it reports progress into a Queue that a
        main-thread pump drains into the console window (Tkinter is not
        thread-safe, so the worker never touches widgets). LAUNCH/COWORK/CLEAN are
        all disabled for the duration so nothing can be started mid-deletion.
        """
        if cowork_running():
            messagebox.showwarning(
                "Quit Claude Desktop first",
                "Claude Desktop is running. Its files are locked and rewritten "
                "while it runs, so cleanup can't proceed.\n\n"
                "Fully quit Claude Desktop (including the tray icon), then click "
                "CLEAN UP EXCESS again.")
            return

        data_dir = claude_data_dir()
        targets = cleanup_targets(data_dir)
        if not targets:
            messagebox.showinfo(
                "Nothing to clean",
                "No cleanable files were found in the Claude data directory.")
            return

        # Lock out launching anything while files are being deleted.
        self.launch_btn.config(state="disabled")
        self.cowork_btn.config(state="disabled")
        self.clean_btn.config(state="disabled", bg=self.PANEL,
                              fg=self.MUTED, cursor="arrow")

        # Console-output popup.
        win = tk.Toplevel(self)
        win.title("Cleaning up Claude disk usage")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        # Can't be closed mid-deletion (would orphan the pump + leave the main
        # buttons disabled); re-enabled in finish().
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        txt = tk.Text(win, width=60, height=14, bg=self.PANEL, fg=self.FG,
                      relief="flat", bd=0, font=self.FONT_SM, wrap="word",
                      padx=10, pady=10, state="disabled")
        txt.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        close_btn = tk.Button(
            win, text="Close", font=self.FONT_SM,
            bg=self.PANEL, fg=self.MUTED,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0, padx=12, pady=6,
            state="disabled", command=win.destroy)
        close_btn.pack(pady=10)

        def append(line):
            txt.config(state="normal")
            txt.insert("end", line + "\n")
            txt.see("end")
            txt.config(state="disabled")

        log_q = queue.Queue()
        done = object()

        def worker():
            try:
                run_cleanup(targets, log_q.put, abort_if_running=True)
            except Exception as exc:
                log_q.put("ERROR: %s" % exc)
            log_q.put(done)

        def finish():
            # Re-enable the main window first — this must happen even if the
            # popup is somehow gone, so launching is never left locked.
            self.launch_btn.config(state="normal")
            self.cowork_btn.config(state="normal")
            if win.winfo_exists():
                close_btn.config(state="normal", fg=self.FG)
                win.protocol("WM_DELETE_WINDOW", win.destroy)
            self._measure_disk()

        def pump():
            try:
                while True:
                    item = log_q.get_nowait()
                    if item is done:
                        finish()
                        return
                    append(item)
            except queue.Empty:
                pass
            self.after(80, pump)

        append("Target: %s" % data_dir)
        append("")
        threading.Thread(target=worker, daemon=True).start()
        pump()

    def _browse(self):
        initial = self.project_dir.get() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="Select Project Directory",
            initialdir=initial,
            mustexist=True,
        )
        if chosen:
            self.project_dir.set(chosen)

    # ── Reduce-confirmations window ─────────────────────────────────────────────

    MC_EXPLAIN = {
        "acceptEdits":
            "Lets Claude create, edit, and delete files anywhere in this project "
            "without asking. Everything here is git-tracked, so any change is "
            "revertible. Claude will still ask before running shell commands or "
            "changing files outside the project.",
        "bypassPermissions":
            "⚠  Silences EVERY prompt and disables safety checks. Only use in "
            "throwaway / fully trusted environments — not recommended on your "
            "main machine.",
    }

    def _open_min_confirm(self):
        """Open the modal 'Reduce Claude confirmations' window.

        Shows the current effective defaultMode (and where it's set), lets the
        user pick a level (acceptEdits / bypassPermissions) and a scope (this
        project → settings.local.json, or globally → ~/.claude/settings.json),
        and Apply merges only permissions.defaultMode into the chosen file.
        """
        proj = self.project_dir.get().strip()

        win = tk.Toplevel(self)
        win.title("Reduce Claude confirmations")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.transient(self)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        self._mc_win = win

        pad = tk.Frame(win, bg=self.BG, padx=18, pady=16)
        pad.pack(fill="both", expand=True)

        tk.Label(pad, text="🛡  Reduce Claude confirmations", font=self.FONT_HD,
                 bg=self.BG, fg="#00c8ff").pack(anchor="w")

        # Current effective state.
        mode, scope = effective_confirm_state(proj)
        if mode:
            state_text = "Current: %s  (set at the %s scope)" % (mode, scope)
        else:
            state_text = "Current: no minimizing mode set — Claude asks normally."
        self._mc_state_text = state_text
        tk.Label(pad, text=state_text, font=self.FONT_SM, bg=self.BG,
                 fg=self.MUTED, wraplength=440, justify="left").pack(
                     anchor="w", pady=(4, 14))

        # Level radios.
        tk.Label(pad, text="LEVEL", font=self.FONT_SM, bg=self.BG,
                 fg=self.MUTED).pack(anchor="w")
        self._mc_level = tk.StringVar(value="acceptEdits")
        for val, label in (("acceptEdits", "Auto-accept edits  (recommended)"),
                           ("bypassPermissions", "Skip all confirmations")):
            tk.Radiobutton(
                pad, text=label, variable=self._mc_level, value=val,
                command=self._mc_update_explain,
                font=self.FONT_SM, bg=self.BG, fg=self.FG,
                activebackground=self.BG, activeforeground=self.FG,
                selectcolor=self.PANEL, relief="flat", bd=0, cursor="hand2",
            ).pack(anchor="w")

        # Dynamic explanation.
        self._mc_explain = tk.Label(
            pad, text="", font=self.FONT_SM, bg=self.PANEL, fg=self.FG,
            wraplength=440, justify="left", padx=10, pady=8)
        self._mc_explain.pack(fill="x", pady=(6, 14))
        self._mc_update_explain()

        # Scope radios.
        tk.Label(pad, text="SCOPE", font=self.FONT_SM, bg=self.BG,
                 fg=self.MUTED).pack(anchor="w")
        self._mc_scope = tk.StringVar(value="local" if proj else "user")
        proj_rb = tk.Radiobutton(
            pad, text="This project  (.claude/settings.local.json)",
            variable=self._mc_scope, value="local",
            font=self.FONT_SM, bg=self.BG, fg=self.FG,
            activebackground=self.BG, activeforeground=self.FG,
            selectcolor=self.PANEL, relief="flat", bd=0, cursor="hand2",
        )
        proj_rb.pack(anchor="w")
        tk.Radiobutton(
            pad, text="Globally  (~/.claude/settings.json)",
            variable=self._mc_scope, value="user",
            font=self.FONT_SM, bg=self.BG, fg=self.FG,
            activebackground=self.BG, activeforeground=self.FG,
            selectcolor=self.PANEL, relief="flat", bd=0, cursor="hand2",
        ).pack(anchor="w")
        if not proj:
            proj_rb.config(state="disabled", cursor="arrow")
            tk.Label(pad, text="No project folder selected — only the global "
                               "scope can be set.", font=self.FONT_SM, bg=self.BG,
                     fg=self.MUTED, wraplength=440, justify="left").pack(
                         anchor="w", pady=(2, 0))

        # Result line (filled in after Apply).
        self._mc_result = tk.Label(pad, text="", font=self.FONT_SM, bg=self.BG,
                                   fg=self.MUTED, wraplength=440, justify="left")
        self._mc_result.pack(anchor="w", pady=(10, 0))

        # Buttons.
        btn_row = tk.Frame(pad, bg=self.BG)
        btn_row.pack(fill="x", pady=(14, 0))
        self._mc_apply_btn = tk.Button(
            btn_row, text="Apply", font=self.FONT_SM,
            bg="#00c8ff", fg="#0d0f14",
            activebackground="#33d4ff", activeforeground="#0d0f14",
            relief="flat", cursor="hand2", bd=0, padx=16, pady=6,
            command=self._mc_apply)
        self._mc_apply_btn.pack(side="right")
        tk.Button(
            btn_row, text="Close", font=self.FONT_SM,
            bg=self.PANEL, fg=self.MUTED,
            activebackground=self.BORDER, activeforeground=self.FG,
            relief="flat", cursor="hand2", bd=0, padx=16, pady=6,
            command=win.destroy).pack(side="right", padx=(0, 8))

    def _mc_update_explain(self):
        """Swap the explanation label to match the selected level."""
        self._mc_explain.config(text=self.MC_EXPLAIN[self._mc_level.get()])

    def _mc_apply(self):
        """Apply the selected defaultMode to the selected scope's settings file."""
        mode = self._mc_level.get()
        scope = self._mc_scope.get()
        proj = self.project_dir.get().strip()
        path = claude_settings_paths(proj).get(scope)
        if not path:
            self._mc_result.config(
                text="No project folder selected — choose the Globally scope.",
                fg="#f85149")
            return
        try:
            apply_default_mode(path, mode)
        except Exception as exc:
            self._mc_result.config(text="Failed to write %s:\n%s" % (path, exc),
                                   fg="#f85149")
            return
        self._mc_result.config(
            text='Wrote defaultMode="%s" to:\n%s\nTakes effect on the next '
                 'Claude launch.' % (mode, path),
            fg="#3fb950")

    def _save_prefs(self):
        """Persist current state. Side-effect-free: does NOT touch recent_dirs.

        Called on every checkbox toggle, on window close, on Cowork launch, and
        on LAUNCH. Recent-dir promotion is a separate, launch-only action
        (_promote_recent_dir) so merely setting a project dir doesn't reorder
        the recents before the user actually launches.
        """
        proj = self.project_dir.get().strip()
        save_prefs({
            "profile": self.selected.get(),
            "project_dir": proj,
            "recent_dirs": self.recent_dirs,
            "extra_powershell": self.extra_ps.get(),
            "open_explorer": self.open_explorer.get(),
            "disk_limit_gb": self.disk_limit_gb,
        })

    def _promote_recent_dir(self):
        """Move the current project dir to the front of recent_dirs (max 8).

        Launch-only: called from _launch just before _save_prefs so the recents
        reflect what was actually launched, not every transient path edit.
        """
        proj = self.project_dir.get().strip()
        if not proj:
            return
        if proj in self.recent_dirs:
            self.recent_dirs.remove(proj)
        self.recent_dirs.insert(0, proj)
        self.recent_dirs = self.recent_dirs[:8]

    def _on_close(self):
        """WM close handler: persist current state, then destroy the window."""
        self._save_prefs()
        self.destroy()

    def _launch_cowork(self):
        """Launch Claude Cowork (the desktop app) if it isn't already running.

        If the app is already up, just bring its window to the foreground —
        opening the cowork/new deep link again would spawn another session.
        """
        self.cowork_btn.config(state="disabled", text="…  CHECKING")
        self.update_idletasks()
        try:
            if cowork_running():
                # Activating the AUMID of a running single-instance app
                # focuses the existing window.
                subprocess.Popen(["explorer.exe", COWORK_AUMID])
            else:
                os.startfile(COWORK_URL)
        except Exception as exc:
            self.cowork_btn.config(state="normal",
                                   text="⬡  CLAUDE COWORK (DESKTOP APP)")
            messagebox.showerror("Cowork launch failed", str(exc))
            return
        self._save_prefs()
        self.destroy()

    def _launch(self):
        idx = self.selected.get()
        profile = PROFILES[idx]
        proj = self.project_dir.get().strip()

        # Validate project dir if set
        if proj and not os.path.isdir(proj):
            messagebox.showerror("Invalid directory", f"Directory not found:\n{proj}")
            return

        self._promote_recent_dir()
        self._save_prefs()
        apply_env(profile)

        # Build the PowerShell command: cd to project dir first if set
        if proj:
            # Escape backslashes for PowerShell
            ps_proj = proj.replace("'", "''")
            cmd = f"Set-Location '{ps_proj}'; claude"
        else:
            cmd = "claude"

        ps_cmd = ["powershell.exe", "-NoExit", "-Command", cmd]

        try:
            subprocess.Popen(ps_cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as exc:
            messagebox.showerror("Launch failed", str(exc))
            return

        # Optional second plain PowerShell in the project dir
        if self.extra_ps.get():
            if proj:
                ps_proj = proj.replace("'", "''")
                extra_cmd = ["powershell.exe", "-NoExit", "-Command",
                             f"Set-Location '{ps_proj}'"]
            else:
                extra_cmd = ["powershell.exe", "-NoExit"]
            try:
                subprocess.Popen(extra_cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
            except Exception as exc:
                messagebox.showerror("Extra shell failed", str(exc))
                return

        # Optional File Explorer window in the project dir
        if self.open_explorer.get():
            # explorer.exe silently falls back to Documents on forward-slash
            # paths — normalize to backslashes first.
            explorer_cmd = ["explorer.exe", os.path.normpath(proj)] if proj else ["explorer.exe"]
            try:
                subprocess.Popen(explorer_cmd)
            except Exception as exc:
                messagebox.showerror("Explorer failed", str(exc))
                return

        self.destroy()

    def _center(self):
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ClaudeSwitcher()
    app.mainloop()
