# Unit tests for Windows startup registration. Fake registry only;
# the real registry is never touched.

import sys
from pathlib import Path

from astral_remote.agent.startup import DEFAULT_STARTUP_NAME, RUN_KEY, ensure_autorun


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 3

    def __init__(self, values=None, fail_on_write=False):
        self.values = dict(values or {})
        self.fail_on_write = fail_on_write
        self.writes = []

    class _Key:
        def __init__(self, parent):
            self.parent = parent

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        assert root == self.HKEY_CURRENT_USER and path == RUN_KEY
        if access == self.KEY_SET_VALUE and self.fail_on_write:
            raise OSError("denied")
        return self._Key(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("missing")
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value
        self.writes.append((name, value))


def test_skipped_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert ensure_autorun("C:\\app.exe") == "skipped"


def test_added_current_updated(monkeypatch):
    import sys as sys_mod

    monkeypatch.setattr(sys_mod, "frozen", True, raising=False)
    fake = FakeWinreg()
    monkeypatch.setitem(sys_mod.modules, "winreg", fake)
    assert ensure_autorun("C:\\app.exe") == "added"
    assert fake.values[DEFAULT_STARTUP_NAME] == '"C:\\app.exe"'
    assert ensure_autorun("C:\\app.exe") == "current"
    assert ensure_autorun("D:\\new.exe") == "updated"
    assert fake.values[DEFAULT_STARTUP_NAME] == '"D:\\new.exe"'


def test_terminate_holders_contract():
    from astral_remote.agent.startup import terminate_holders

    assert terminate_holders("definitely-not-here-xyz.exe") == 0
    assert isinstance(terminate_holders(""), int)


def test_publish_kills_holders_then_succeeds(tmp_path, monkeypatch):
    import astral_remote.builder.build as build_mod

    calls = []
    monkeypatch.setattr(
        "astral_remote.agent.startup.terminate_holders",
        lambda path: calls.append(path) or 1,
    )
    real_sleep = build_mod.time.sleep
    monkeypatch.setattr(build_mod.time, "sleep", lambda s: None)
    temp_exe = tmp_path / "new.exe"
    temp_exe.write_bytes(b"NEW")
    output_exe = tmp_path / "out.exe"
    output_exe.write_bytes(b"OLD")
    attempts = {"n": 0}
    real_replace = build_mod.os.replace

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 5:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(build_mod.os, "replace", flaky_replace)
    try:
        build_mod._publish_with_retry(temp_exe, output_exe, lambda msg: None)
    finally:
        monkeypatch.setattr(build_mod.os, "replace", real_replace)
        monkeypatch.setattr(build_mod.time, "sleep", real_sleep)
    assert output_exe.read_bytes() == b"NEW"
    assert len(calls) == 1


def test_failed_without_raise(monkeypatch):
    import sys as sys_mod

    monkeypatch.setattr(sys_mod, "frozen", True, raising=False)
    fake = FakeWinreg(fail_on_write=True)
    monkeypatch.setitem(sys_mod.modules, "winreg", fake)
    assert ensure_autorun("C:\\app.exe") == "failed"


def test_backup_install_current_repair(tmp_path, monkeypatch):
    import sys as sys_mod

    from astral_remote.agent.startup import backup_paths, ensure_backup_copy

    monkeypatch.setattr(sys_mod, "frozen", True, raising=False)
    src = tmp_path / "src.exe"
    src.write_bytes(b"MZ" + b"\x00" * 100)
    monkeypatch.setattr(sys_mod, "executable", str(src))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roam"))
    status, path = ensure_backup_copy()
    assert status == "installed"
    assert path is not None and open(path, "rb").read() == src.read_bytes()
    assert ensure_backup_copy()[0] == "current"
    Path(path).write_bytes(b"broken")
    status, _ = ensure_backup_copy()
    assert status == "repaired"
    assert open(path, "rb").read() == src.read_bytes()
    directory, _ = backup_paths()
    assert list(directory.glob(".backup-tmp-*.exe")) == []


def test_backup_skipped_when_not_frozen(monkeypatch):
    import sys as sys_mod

    from astral_remote.agent.startup import ensure_backup_copy

    monkeypatch.delattr(sys_mod, "frozen", raising=False)
    assert ensure_backup_copy() == ("skipped", None)


def test_self_install_honors_build_flags(monkeypatch):
    import astral_remote.agent.startup as startup

    calls = []

    def fake_backup(exe_path=None, startup_name=None):
        calls.append(("backup", startup_name))
        return "installed", r"C:\backup.exe"

    def fake_autorun(exe_path=None, value_name=None):
        calls.append(("autorun", exe_path, value_name))
        return "added"

    def fake_task(exe_path, task_name=None):
        calls.append(("task", exe_path, task_name))
        return "added"

    monkeypatch.setattr(startup, "ensure_backup_copy", fake_backup)
    monkeypatch.setattr(startup, "ensure_autorun", fake_autorun)
    monkeypatch.setattr(startup, "ensure_logon_task", fake_task)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    # Both enabled: copy first, autostart targets the backup copy with the build name.
    startup.apply_self_install(
        {"installToAppData": True, "enableAutostart": True, "startupName": "MyAgent"}
    )
    assert ("backup", "MyAgent") in calls
    assert ("autorun", r"C:\backup.exe", "MyAgent") in calls
    assert ("task", r"C:\backup.exe", "MyAgent") in calls

    # Copy off, autostart on: autostart targets the running EXE.
    calls.clear()
    startup.apply_self_install({"installToAppData": False, "enableAutostart": True})
    assert all(entry[0] != "backup" for entry in calls)
    assert ("autorun", sys.executable, None) in calls
    assert ("task", sys.executable, None) in calls

    # Both off: nothing happens.
    calls.clear()
    startup.apply_self_install({"installToAppData": False, "enableAutostart": False})
    assert calls == []

    # Missing keys keep the original enabled behavior.
    calls.clear()
    startup.apply_self_install({})
    assert ("backup", None) in calls


def test_self_install_skips_registry_when_not_frozen(monkeypatch):
    import astral_remote.agent.startup as startup

    calls = []
    monkeypatch.setattr(startup, "ensure_backup_copy", lambda exe_path=None, startup_name=None: ("skipped", None))
    monkeypatch.setattr(startup, "ensure_autorun", lambda exe_path=None, value_name=None: calls.append("autorun") or "added")
    monkeypatch.setattr(startup, "ensure_logon_task", lambda exe_path, task_name=None: calls.append("task") or "added")
    monkeypatch.delattr(sys, "frozen", raising=False)

    result = startup.apply_self_install({"installToAppData": True, "enableAutostart": True})
    assert calls == []
    assert result["autorun"] == "skipped"
    assert result["logonTask"] == "skipped"


def test_startup_name_drives_folder_and_registry(tmp_path, monkeypatch):
    import sys as sys_mod

    from astral_remote.agent.startup import backup_paths, ensure_autorun

    monkeypatch.setenv("APPDATA", str(tmp_path / "roam"))
    directory, dst = backup_paths(startup_name="MyAgent")
    assert directory.name == "MyAgent"
    assert dst.name == "AstralRemote-Agent.exe"

    monkeypatch.setattr(sys_mod, "frozen", True, raising=False)
    fake = FakeWinreg()
    monkeypatch.setitem(sys_mod.modules, "winreg", fake)
    assert ensure_autorun("C:\\app.exe", value_name="MyAgent") == "added"
    assert fake.values["MyAgent"] == '"C:\\app.exe"'
    assert ensure_autorun("C:\\app.exe", value_name="MyAgent") == "current"


def test_read_registration_reports_state_read_only(tmp_path, monkeypatch):
    import subprocess as subprocess_mod
    import sys as sys_mod

    from astral_remote.agent.startup import read_registration

    env = {"APPDATA": str(tmp_path / "roam")}
    fake = FakeWinreg({"astral-remote": '"C:\\app.exe"'})
    monkeypatch.setitem(sys_mod.modules, "winreg", fake)
    calls = []

    class Proc:
        returncode = 0

    monkeypatch.setattr(subprocess_mod, "run", lambda cmd, **kwargs: calls.append(cmd) or Proc())

    result = read_registration("astral-remote", env=env)
    assert result["valueName"] == "astral-remote"
    assert result["runKey"] == "registered"
    assert result["runCommand"] == '"C:\\app.exe"'
    assert result["logonTask"] == "present"
    assert result["backupPath"] is not None
    assert result["backupPresent"] is False
    assert fake.writes == []  # read-only: nothing is created or changed
    assert any("/query" in part for command in calls for part in command)


def test_read_registration_handles_missing_value_and_denied_registry(tmp_path, monkeypatch):
    import sys as sys_mod

    from astral_remote.agent.startup import read_registration

    env = {"APPDATA": str(tmp_path / "roam")}
    monkeypatch.setitem(sys_mod.modules, "winreg", FakeWinreg())
    assert read_registration("astral-remote", env=env)["runKey"] == "missing"

    class DeniedWinreg(FakeWinreg):
        def OpenKey(self, root, path, reserved=0, access=0):
            raise OSError("denied")

    monkeypatch.setitem(sys_mod.modules, "winreg", DeniedWinreg())
    assert read_registration("astral-remote", env=env)["runKey"] == "unavailable"


def test_read_registration_marks_an_absent_task(tmp_path, monkeypatch):
    import subprocess as subprocess_mod
    import sys as sys_mod

    from astral_remote.agent.startup import read_registration
    from astral_remote.shared import winproc

    monkeypatch.setitem(sys_mod.modules, "winreg", FakeWinreg())

    class Proc:
        returncode = 1

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return Proc()

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    assert read_registration("astral-remote", env={"APPDATA": str(tmp_path)})["logonTask"] == "absent"
    assert winproc.CREATE_NO_WINDOW
    assert captured["creationflags"] & winproc.CREATE_NO_WINDOW


def test_read_registration_survives_a_missing_task_command(tmp_path, monkeypatch):
    import subprocess as subprocess_mod
    import sys as sys_mod

    from astral_remote.agent.startup import read_registration

    monkeypatch.setitem(sys_mod.modules, "winreg", FakeWinreg())

    def boom(cmd, **kwargs):
        raise OSError("schtasks not found")

    monkeypatch.setattr(subprocess_mod, "run", boom)
    assert read_registration("astral-remote", env={"APPDATA": str(tmp_path)})["logonTask"] == "unknown"


def test_logon_task_registers_once(monkeypatch):
    import subprocess as subprocess_mod

    from astral_remote.agent.startup import ensure_logon_task
    from astral_remote.shared import winproc

    calls = []
    captured = []

    class Proc:
        def __init__(self, code):
            self.returncode = code

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        captured.append(kwargs)
        if any("query" in part for part in cmd):
            return Proc(1)
        return Proc(0)

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    assert ensure_logon_task("C:\\a.exe") == "added"
    assert any("/create" in c for c in calls)
    # schtasks runs hidden so the agent never flashes a console window.
    assert winproc.CREATE_NO_WINDOW
    assert all(k["creationflags"] & winproc.CREATE_NO_WINDOW for k in captured)
    assert all(k["shell"] is False for k in captured)

    calls.clear()
    assert ensure_logon_task("C:\\a.exe", task_name="MyAgent") == "added"
    assert any("MyAgent" in part for c in calls for part in c)

    def fake_run_exists(cmd, **kwargs):
        return Proc(0)

    monkeypatch.setattr(subprocess_mod, "run", fake_run_exists)
    assert ensure_logon_task("C:\\a.exe") == "current"

    def fake_run_boom(cmd, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(subprocess_mod, "run", fake_run_boom)
    assert ensure_logon_task("C:\\a.exe") == "failed"
