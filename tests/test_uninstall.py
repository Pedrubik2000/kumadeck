import json
import os
import signal
import subprocess
import sys
import time

from vnlookup.uninstall import (
    cleanup_after_uninstall,
    disk_usage,
    downloaded_data_paths,
    plugin_installed,
    remove_paths,
    spawn_uninstall_cleanup,
    was_uninstalled,
)

PLUGIN = "kumadeck"


def _make_plugin(root, folder="vn-lookup", name=PLUGIN):
    d = root / folder
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": name}))
    return d


class FakeClock:
    """Drives was_uninstalled() without real sleeping."""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _watch(installed_at):
    """Run was_uninstalled() where `installed_at(t)` says whether the
    plugin is present at time t."""
    fc = FakeClock()
    return was_uninstalled(lambda: installed_at(fc.now), poll_s=0.5,
                           confirm_s=15, max_wait_s=120,
                           sleep=fc.sleep, clock=fc.clock)


# ---- decision logic ----------------------------------------------------------

def test_uninstall_detected_when_plugin_disappears_and_stays_gone():
    assert _watch(lambda t: t < 3) is True


def test_update_with_visible_gap_keeps_data():
    # old copy removed at 3s, new copy unzipped at 4s
    assert _watch(lambda t: t < 3 or t >= 4) is False


def test_update_too_fast_to_observe_keeps_data():
    assert _watch(lambda t: True) is False


def test_brief_absence_shorter_than_confirm_window_keeps_data():
    assert _watch(lambda t: not (3 <= t < 17)) is False


# ---- filesystem helpers ------------------------------------------------------

def test_plugin_installed_matches_by_name_in_any_folder(tmp_path):
    assert plugin_installed(str(tmp_path), PLUGIN) is False
    _make_plugin(tmp_path, folder="other", name="Some Other Plugin")
    (tmp_path / "not-a-plugin").mkdir()
    assert plugin_installed(str(tmp_path), PLUGIN) is False
    _make_plugin(tmp_path, folder="renamed-folder")
    assert plugin_installed(str(tmp_path), PLUGIN) is True


def test_plugin_installed_ignores_half_written_plugin_json(tmp_path):
    d = tmp_path / "vn-lookup"
    d.mkdir()
    (d / "plugin.json").write_text('{"name": "Japa')
    assert plugin_installed(str(tmp_path), PLUGIN) is False


def test_unreadable_plugins_root_counts_as_installed(tmp_path):
    assert plugin_installed(str(tmp_path / "missing"), PLUGIN) is True


def test_downloaded_data_keeps_anki_queue(tmp_path):
    for name in ("venv", "models", "dicts", "captures"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.bin").write_bytes(b"x" * 1000)
    (tmp_path / "dictionary.sqlite3").write_bytes(b"x" * 500)
    (tmp_path / "anki_buffer.json").write_text("[]")

    paths = downloaded_data_paths(str(tmp_path))
    assert str(tmp_path / "anki_buffer.json") not in paths
    assert disk_usage(paths) == 4500

    remove_paths(paths)
    assert sorted(os.listdir(tmp_path)) == ["anki_buffer.json"]


def test_downloaded_data_paths_missing_dir(tmp_path):
    assert downloaded_data_paths(str(tmp_path / "missing")) == []


def test_cleanup_removes_every_path_after_uninstall(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    data, settings = tmp_path / "data", tmp_path / "settings"
    data.mkdir()
    settings.mkdir()
    result = cleanup_after_uninstall({
        "plugin_name": PLUGIN, "plugins_root": str(plugins),
        "paths": [str(data), str(settings)],
        "timings": {"poll_s": 0.01, "confirm_s": 0.05, "max_wait_s": 1}})
    assert "removed" in result
    assert not data.exists() and not settings.exists()


# ---- the real detached process -----------------------------------------------

def _spawn_from_parent(tmp_path, plugins, data):
    """Spawn the watcher from a throwaway parent, then SIGKILL that parent
    the way Decky kills the plugin process."""
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]);"
        "from vnlookup.uninstall import spawn_uninstall_cleanup as s;"
        f"s(plugin_name={PLUGIN!r}, plugins_root={str(plugins)!r}, paths=[{str(data)!r}],"
        f" log_path={str(tmp_path / 'watch.log')!r}, python={sys.executable!r},"
        " timings={'poll_s': 0.05, 'confirm_s': 0.5, 'max_wait_s': 5});"
        "time.sleep(60)"
    )
    py_modules = os.path.join(os.path.dirname(__file__), "..", "py_modules")
    parent = subprocess.Popen([sys.executable, "-c", code, py_modules])
    time.sleep(1.0)
    parent.send_signal(signal.SIGKILL)
    parent.wait()


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_detached_watcher_survives_parent_kill_and_cleans_up(tmp_path):
    plugins = tmp_path / "plugins"
    plugin_dir = _make_plugin(plugins)
    data = tmp_path / "data"
    (data / "venv").mkdir(parents=True)

    _spawn_from_parent(tmp_path, plugins, data)
    # Decky deletes the plugin folder after killing the process
    for f in plugin_dir.iterdir():
        f.unlink()
    plugin_dir.rmdir()

    assert _wait_for(lambda: not data.exists())
    assert "removed" in (tmp_path / "watch.log").read_text()


def test_detached_watcher_keeps_data_on_update(tmp_path):
    plugins = tmp_path / "plugins"
    plugin_dir = _make_plugin(plugins)
    data = tmp_path / "data"
    (data / "venv").mkdir(parents=True)

    _spawn_from_parent(tmp_path, plugins, data)
    # update: old copy removed, new copy unzipped straight after
    (plugin_dir / "plugin.json").unlink()
    plugin_dir.rmdir()
    time.sleep(0.2)
    _make_plugin(plugins)

    assert _wait_for(lambda: "kept" in (tmp_path / "watch.log").read_text())
    assert data.exists()


def test_spawn_does_not_run_from_the_plugin_folder(tmp_path):
    # Decky deletes the plugin folder (this file included) while the
    # watcher runs, so it must not be started from a path in there
    proc = spawn_uninstall_cleanup(
        plugin_name=PLUGIN, plugins_root=str(tmp_path), paths=[],
        log_path=str(tmp_path / "log"), python=sys.executable,
        timings={"poll_s": 0.01, "confirm_s": 0.05, "max_wait_s": 1})
    assert not any("uninstall.py" in a for a in proc.args)
    assert proc.wait(timeout=10) == 0
    assert "plugin uninstalled" in (tmp_path / "log").read_text()
