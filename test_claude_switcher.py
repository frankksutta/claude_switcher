"""Headless tests for claude_switcher.pyw.

The app is a single-file Tkinter GUI; per the project's CLAUDE.md these tests
instantiate the window, exercise pure helpers and the settings engine, and tear
the window down — no manual GUI pass needed. The prefs file is backed up and
restored around any test that writes it.

Run: python -m pytest test_claude_switcher.py   (or: python test_claude_switcher.py)
"""
import importlib.util
import json
import os
import shutil
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    """Import claude_switcher.pyw as a module (the .pyw ext blocks plain import)."""
    path = os.path.join(HERE, "claude_switcher.pyw")
    spec = importlib.util.spec_from_file_location("claude_switcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load_module()


@pytest.fixture(scope="session")
def app():
    """One ClaudeSwitcher root shared by every window test in the session.

    Repeated tk.Tk() creation in a single process is flaky on this machine: Tcl
    intermittently fails to re-read init.tcl ("No error"). The first creation is
    always reliable, so we create exactly one root for the whole session. The
    prefs file is seeded (recent dirs, so the recent menu exists) and the real
    file is backed up + restored around the session. Tests must not destroy this
    root; the close test spies on .destroy instead.
    """
    path = cs.PREFS_FILE
    backup = path + ".bak_sess"
    existed = os.path.exists(path)
    if existed:
        shutil.copy2(path, backup)
    cs.save_prefs({"recent_dirs": ["C:/foo/alpha", "C:/foo/beta"]})
    a = None
    try:
        try:
            a = cs.ClaudeSwitcher()
        except Exception as exc:  # no display / Tcl race
            pytest.skip("cannot create Tk window: %s" % exc)
        a.update_idletasks()
        yield a
    finally:
        if a is not None:
            try:
                a.destroy()
            except Exception:
                pass
        if existed:
            shutil.move(backup, path)
        elif os.path.exists(path):
            os.remove(path)


# ── Phase 1: persistence + highlight + tooltips ─────────────────────────────────

def test_save_prefs_persists_checkboxes(app):
    app.extra_ps.set(True)
    app.open_explorer.set(True)
    app._save_prefs()
    saved = cs.load_prefs()
    assert saved["extra_powershell"] is True
    assert saved["open_explorer"] is True


def test_save_prefs_does_not_promote_recent(app):
    app.recent_dirs = ["A", "B"]
    app.project_dir.set("C")  # a new dir, not launched
    app._save_prefs()
    assert app.recent_dirs == ["A", "B"], "save must not reorder/insert recents"


def test_promote_recent_dir_moves_to_front(app):
    app.recent_dirs = ["A", "B"]
    app.project_dir.set("B")
    app._promote_recent_dir()
    assert app.recent_dirs[0] == "B"
    app.project_dir.set("NEW")
    app._promote_recent_dir()
    assert app.recent_dirs[0] == "NEW"
    assert len(app.recent_dirs) <= 8


def test_on_close_persists_then_destroys(app):
    # Spy on destroy so we don't tear down the shared root.
    calls = {"destroyed": False}
    real_destroy = app.destroy
    app.destroy = lambda: calls.__setitem__("destroyed", True)
    try:
        app.extra_ps.set(True)
        app._on_close()
        assert cs.load_prefs()["extra_powershell"] is True  # persisted
        assert calls["destroyed"] is True                   # then destroy called
    finally:
        app.destroy = real_destroy


def test_recent_highlight(app):
    # Recent menu was built from the seeded recents (alpha, beta).
    assert hasattr(app, "recent_buttons") and len(app.recent_buttons) == 2
    app.project_dir.set("C:/foo/alpha")
    app.update_idletasks()
    bgs = {path: str(btn.cget("bg")) for btn, path in app.recent_buttons}
    assert bgs["C:/foo/alpha"] == app.RECENT_HL
    assert bgs["C:/foo/beta"] == app.BORDER
    # non-recent path → none highlighted
    app.project_dir.set("C:/somewhere/else")
    app.update_idletasks()
    for btn, _ in app.recent_buttons:
        assert str(btn.cget("bg")) == app.BORDER


def test_tooltip_provider_and_lifecycle(app):
    import tkinter as tk
    # Entry provider: basename when set, "(no folder)" when empty.
    app.project_dir.set(r"C:\foo\my-project")
    prov = (lambda: (os.path.basename(os.path.normpath(app.project_dir.get().strip()))
                     or "(no folder)") if app.project_dir.get().strip() else "(no folder)")
    assert prov() == "my-project"
    app.project_dir.set("")
    assert prov() == "(no folder)"

    # attach_tooltip lifecycle: <Enter> creates a Toplevel, <Leave> destroys it.
    btn = tk.Button(app, text="x")
    btn.pack()
    app.update_idletasks()
    cs.attach_tooltip(btn, lambda: "C:/full/path")
    before = len(btn.winfo_children())
    btn.event_generate("<Enter>")
    app.update_idletasks()
    assert len(btn.winfo_children()) == before + 1  # tooltip Toplevel exists
    btn.event_generate("<Leave>")
    app.update_idletasks()
    assert len(btn.winfo_children()) == before  # gone
    btn.destroy()


# ── Phase 2: settings engine (pure logic) ───────────────────────────────────────

def test_settings_paths():
    p = cs.claude_settings_paths(r"C:\proj")
    assert p["user"].endswith(os.path.join(".claude", "settings.json"))
    assert p["project"].endswith(os.path.join("proj", ".claude", "settings.json"))
    assert p["local"].endswith(os.path.join("proj", ".claude", "settings.local.json"))
    # empty project dir → project/local are None, user still resolved
    empty = cs.claude_settings_paths("")
    assert empty["project"] is None and empty["local"] is None
    assert empty["user"].endswith(os.path.join(".claude", "settings.json"))


def test_read_default_mode(tmp_path):
    missing = tmp_path / "nope.json"
    assert cs.read_default_mode(str(missing)) is None
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}))
    assert cs.read_default_mode(str(f)) == "acceptEdits"
    # invalid json → None, not a crash
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cs.read_default_mode(str(bad)) is None


def test_apply_default_mode_merges_and_preserves(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    local = claude_dir / "settings.local.json"
    local.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2))
    cs.apply_default_mode(str(local), "acceptEdits")
    data = json.loads(local.read_text())
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(ls)"], "existing allow list preserved"
    # effective state now resolves to local/acceptEdits
    mode, scope = cs.effective_confirm_state(str(tmp_path))
    assert (mode, scope) == ("acceptEdits", "local")


def test_apply_default_mode_creates_missing_file(tmp_path):
    target = tmp_path / "sub" / ".claude" / "settings.json"
    cs.apply_default_mode(str(target), "bypassPermissions")
    assert target.is_file()
    assert json.loads(target.read_text())["permissions"]["defaultMode"] == "bypassPermissions"


def test_effective_precedence(tmp_path):
    # No settings anywhere under this temp project → (None, None) for project/local.
    paths = cs.claude_settings_paths(str(tmp_path))
    assert cs.read_default_mode(paths["local"]) is None
    # seed project (committed) then local (gitignored) — local must win.
    cs.apply_default_mode(paths["project"], "bypassPermissions")
    assert cs.effective_confirm_state(str(tmp_path)) == ("bypassPermissions", "project")
    cs.apply_default_mode(paths["local"], "acceptEdits")
    assert cs.effective_confirm_state(str(tmp_path)) == ("acceptEdits", "local")


# ── Phase 3: Min. Confirm. button + window ──────────────────────────────────────

def _toplevel_count(app):
    import tkinter as tk
    return sum(1 for c in app.winfo_children() if isinstance(c, tk.Toplevel))


def test_min_confirm_window_builds(app):
    app.project_dir.set("")  # no project → "This project" scope disabled
    before = _toplevel_count(app)
    app._open_min_confirm()
    app.update_idletasks()
    assert _toplevel_count(app) == before + 1
    # Widgets/vars exist.
    assert app._mc_level.get() == "acceptEdits"        # recommended preselected
    assert app._mc_scope.get() == "user"               # no project → global default
    assert str(app._mc_apply_btn.cget("text")) == "Apply"
    # Current-state line matches the engine.
    mode, scope = cs.effective_confirm_state("")
    expected = ("Current: %s  (set at the %s scope)" % (mode, scope) if mode
                else "Current: no minimizing mode set — Claude asks normally.")
    assert app._mc_state_text == expected
    # Explanation swaps with the level var.
    app._mc_level.set("acceptEdits")
    app._mc_update_explain()
    edits_txt = str(app._mc_explain.cget("text"))
    app._mc_level.set("bypassPermissions")
    app._mc_update_explain()
    bypass_txt = str(app._mc_explain.cget("text"))
    assert edits_txt != bypass_txt
    assert "⚠" in bypass_txt
    app._mc_win.destroy()


def test_min_confirm_apply_this_project(app, tmp_path):
    app.project_dir.set(str(tmp_path))
    # pre-seed an allow list to prove merge preserves it
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2))
    app._open_min_confirm()
    app.update_idletasks()
    app._mc_level.set("acceptEdits")
    app._mc_scope.set("local")
    app._mc_apply()
    data = json.loads((claude_dir / "settings.local.json").read_text())
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(ls)"]
    assert "next" in str(app._mc_result.cget("text")).lower()  # success line shown
    app._mc_win.destroy()


def test_min_confirm_apply_globally(app, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # pre-seed a global file with an unrelated key to prove preservation
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(
        json.dumps({"theme": "dark"}, indent=2))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    app.project_dir.set("")
    app._open_min_confirm()
    app.update_idletasks()
    app._mc_level.set("bypassPermissions")
    app._mc_scope.set("user")
    app._mc_apply()
    data = json.loads((fake_home / ".claude" / "settings.json").read_text())
    assert data["permissions"]["defaultMode"] == "bypassPermissions"
    assert data["theme"] == "dark"  # pre-existing key preserved
    app._mc_win.destroy()


def test_help_text_mentions_reduce_confirmations():
    import inspect
    src = inspect.getsource(cs.ClaudeSwitcher._show_help)
    assert "REDUCE CONFIRMATIONS" in src


# ── Profiles config layer + publishable-source scrub ────────────────────────────

# Personal tokens that must never appear in the published source or generic
# default. The real account profiles live in the gitignored profiles JSON.
_PERSONAL_TOKENS = ("frank", "4nt.org", "@4nt")


def test_source_has_no_personal_tokens():
    """claude_switcher.pyw must be publishable — no real emails/usernames."""
    src = open(os.path.join(HERE, "claude_switcher.pyw"), encoding="utf-8").read().lower()
    hits = [t for t in _PERSONAL_TOKENS if t in src]
    assert not hits, "personal tokens leaked into source: %s" % hits


def test_generic_default_profiles_are_clean():
    blob = json.dumps(cs.GENERIC_DEFAULT_PROFILES).lower()
    assert cs._valid_profile_list(cs.GENERIC_DEFAULT_PROFILES)
    assert not any(t in blob for t in _PERSONAL_TOKENS)


def test_load_profiles_generates_default_when_missing(tmp_path):
    target = tmp_path / "profiles.json"
    out = cs.load_profiles(str(target))
    assert target.is_file()                       # file written
    assert len(out) == len(cs.GENERIC_DEFAULT_PROFILES) == 3
    written = target.read_text(encoding="utf-8").lower()
    assert not any(t in written for t in _PERSONAL_TOKENS)
    # config_dir is expanded on load (~ resolved), stored unexpanded on disk.
    assert "~" not in out[0]["config_dir"]
    assert "~" in json.loads(target.read_text())[0]["config_dir"]


def test_load_profiles_reads_custom(tmp_path):
    target = tmp_path / "profiles.json"
    custom = [{
        "label": "Mine", "tag": "X", "color": "#fff",
        "config_dir": "~/.claude-mine", "mode": "claude",
        "env_set": {}, "env_remove": [],
    }]
    target.write_text(json.dumps(custom), encoding="utf-8")
    out = cs.load_profiles(str(target))
    assert len(out) == 1 and out[0]["label"] == "Mine"
    assert out[0]["config_dir"] == os.path.expanduser("~/.claude-mine")


def test_load_profiles_invalid_falls_back(tmp_path):
    target = tmp_path / "profiles.json"
    target.write_text("{not json", encoding="utf-8")
    out = cs.load_profiles(str(target))           # must not raise
    assert len(out) == 3                          # generic default used in memory
    # an existing-but-invalid file is PRESERVED, never clobbered (no data loss on
    # a hand-edit typo) — only a *missing* file is created.
    assert target.read_text(encoding="utf-8") == "{not json"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
