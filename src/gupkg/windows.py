"""Provide the Windows operating-system boundary for package installation.

Shortcut, junction, registry, environment-notification, elevation, and console
operations are isolated here so package workflows can express intent without
mixing platform mechanics into configuration or orchestration.

Implementation Approach
-----------------------
The module uses standard Windows APIs where available and small subprocess
fallbacks where Windows exposes the required behavior through system tools.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .core import Scope, normalize_path

if os.name == "nt":
    import winreg
else:
    winreg = None  # type: ignore[assignment]


def require_winreg() -> Any:
    """Return the :mod:`winreg` module or raise a platform error.

    Returns
    -------
    Any
        The imported :mod:`winreg` module.

    Raises
    ------
    OSError
        If the current interpreter does not provide :mod:`winreg`.

    """
    if winreg is None:
        raise OSError("winreg is only available on Windows.")
    return winreg


def _run_hidden(command: List[str]) -> subprocess.CompletedProcess:
    """Run one Windows command without opening a console window.

    Parameters
    ----------
    command : List[str]
        Command-line tokens to execute.

    Returns
    -------
    subprocess.CompletedProcess
        The completed subprocess result.

    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _escape_powershell_single_quoted(value: str) -> str:
    """Escape text for a PowerShell single-quoted string literal.

    Parameters
    ----------
    value : str
        Text that will be embedded in PowerShell.

    Returns
    -------
    str
        The same text with apostrophes doubled.

    """
    return value.replace("'", "''")


def create_shortcut(
    shortcut_path: Path,
    target_path: str,
    *,
    arguments: str = "",
    working_directory: str = "",
    icon_location: str = "",
    description: str = "",
) -> None:
    """Create one ``.lnk`` file through PowerShell automation.

    Parameters
    ----------
    shortcut_path : Path
        Full ``.lnk`` path to create.
    target_path : str
        Executable path the shortcut should launch.
    arguments : str
        Optional command-line arguments.
    working_directory : str
        Optional working directory.
    icon_location : str
        Optional ``path,index`` icon reference.
    description : str
        Optional description shown by Windows.

    Raises
    ------
    RuntimeError
        If PowerShell reports a shortcut-creation failure.

    """
    ps_command = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{_escape_powershell_single_quoted(str(shortcut_path))}')
$Shortcut.TargetPath = '{_escape_powershell_single_quoted(target_path)}'
$Shortcut.Arguments = '{_escape_powershell_single_quoted(arguments)}'
$Shortcut.WorkingDirectory = '{_escape_powershell_single_quoted(working_directory)}'
$Shortcut.IconLocation = '{_escape_powershell_single_quoted(icon_location)}'
$Shortcut.Description = '{_escape_powershell_single_quoted(description)}'
$Shortcut.Save()
"""
    result = _run_hidden(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_command,
        ]
    )
    if result.returncode != 0:
        error_text = (
            result.stderr or result.stdout or "unknown PowerShell shortcut error"
        ).strip()
        raise RuntimeError(error_text)


def create_junction(source: Path, target: Path) -> None:
    r"""Create or replace one NTFS junction.

    Parameters
    ----------
    source : Path
        Path where the junction should be created.
    target : Path
        Existing directory the junction should reference.

    Raises
    ------
    RuntimeError
        If the junction cannot be created safely.

    """
    if not target.exists() or not target.is_dir():
        raise RuntimeError(
            f"Junction target does not exist or is not a directory: {target}"
        )

    if os.path.lexists(str(source)):
        if is_junction(source):
            os.rmdir(str(source))
        else:
            raise RuntimeError(
                f"{source} already exists and is not a junction; refusing to overwrite."
            )

    result = _run_hidden(["cmd", "/c", "mklink", "/J", str(source), str(target)])
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "mklink /J failed").strip()
        raise RuntimeError(error_text)


def _win_get_reparse_tag(path: Path) -> Optional[int]:
    """Read the reparse tag for one filesystem entry.

    Parameters
    ----------
    path : Path
        Filesystem entry to inspect.

    Returns
    -------
    Optional[int]
        The integer reparse tag, or ``None`` when the tag cannot be read.

    """
    try:
        import ctypes
        from ctypes import wintypes

        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        OPEN_EXISTING = 3
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        FSCTL_GET_REPARSE_POINT = 0x000900A8

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        device_io_control = ctypes.windll.kernel32.DeviceIoControl
        device_io_control.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        device_io_control.restype = wintypes.BOOL

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle_value = wintypes.HANDLE(-1).value
        if handle == invalid_handle_value:
            return None

        try:
            buffer = ctypes.create_string_buffer(16 * 1024)
            returned = wintypes.DWORD(0)
            ok = device_io_control(
                handle,
                FSCTL_GET_REPARSE_POINT,
                None,
                0,
                buffer,
                len(buffer),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return int.from_bytes(buffer.raw[0:4], "little", signed=False)
        finally:
            close_handle(handle)
    except Exception:
        return None


def is_junction(path: Path) -> bool:
    """Return whether one path is an NTFS junction.

    Parameters
    ----------
    path : Path
        Filesystem entry to inspect.

    Returns
    -------
    bool
        ``True`` when *path* exists and is a junction; otherwise ``False``.

    """
    try:
        if hasattr(os.path, "isjunction"):
            return os.path.isjunction(str(path))  # type: ignore[attr-defined]

        if not os.path.isdir(str(path)):
            return False

        io_reparse_tag_mount_point = 0xA0000003
        return _win_get_reparse_tag(path) == io_reparse_tag_mount_point
    except Exception:
        return False


def get_junction_target(path: Path) -> Optional[Path]:
    """Resolve the target of one junction path.

    Parameters
    ----------
    path : Path
        Junction path to inspect.

    Returns
    -------
    Optional[Path]
        The normalized target path, or ``None`` when the target cannot be read.

    """
    try:
        return normalize_path(os.readlink(str(path)))
    except (OSError, AttributeError):
        return None


def environment_registry_location(scope: Scope) -> Tuple[Any, str]:
    """Return the registry location used for one environment scope.

    Parameters
    ----------
    scope : Scope
        Installation scope whose environment location is needed.

    Returns
    -------
    Tuple[Any, str]
        A tuple ``(root_hkey_or_none, subkey)``.

    """
    if scope == Scope.USER:
        return (
            winreg.HKEY_CURRENT_USER if winreg is not None else None,
            r"Environment",
        )
    return (
        winreg.HKEY_LOCAL_MACHINE if winreg is not None else None,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )


def read_registry_value(root: Any, subkey: str, name: str) -> Tuple[Any, int]:
    """Read one registry value.

    Parameters
    ----------
    root : Any
        Registry hive constant.
    subkey : str
        Registry key path below *root*.
    name : str
        Value name to read.

    Returns
    -------
    Tuple[Any, int]
        Tuple ``(value, registry_type)`` from ``QueryValueEx``.

    Raises
    ------
    OSError
        If the value cannot be read.

    """
    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_READ) as key:
        return reg.QueryValueEx(key, name)


def write_registry_value(
    root: Any, subkey: str, name: str, value: str, reg_type: int
) -> None:
    """Write one registry value.

    Parameters
    ----------
    root : Any
        Registry hive constant.
    subkey : str
        Registry key path below *root*.
    name : str
        Value name to write.
    value : str
        Value data to store.
    reg_type : int
        ``winreg`` registry type constant.

    Raises
    ------
    OSError
        If the value cannot be written.

    """
    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, name, 0, reg_type, value)


def broadcast_environment_change() -> None:
    """Notify Windows that environment values changed.

    Raises
    ------
    OSError
        If Windows does not accept the broadcast notification.

    """
    import ctypes
    from ctypes import wintypes

    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002

    send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
    send_message_timeout.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send_message_timeout.restype = wintypes.LPARAM

    # SendMessageTimeoutW writes a DWORD_PTR result. ctypes.c_size_t is the
    # portable pointer-sized unsigned type, unlike wintypes.ULONG_PTR, which
    # is absent from some supported Python builds.
    result = ctypes.c_size_t(0)

    # LPARAM is an integer type, so obtain the Environment string address via
    # c_void_p rather than casting directly to a non-pointer ctypes type.
    environment_name = ctypes.c_wchar_p("Environment")
    environment_lparam = ctypes.cast(environment_name, ctypes.c_void_p).value
    ok = send_message_timeout(
        hwnd_broadcast,
        wm_settingchange,
        0,
        environment_lparam,
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )
    if not ok:
        raise OSError("SendMessageTimeoutW failed")


def is_current_user_admin() -> bool:
    """Return whether the current process has Administrator privileges.

    Returns
    -------
    bool
        ``True`` when the current process is elevated; otherwise ``False``.

    """
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(arguments: list[str]) -> bool:
    """Request one elevated process using an argument vector."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        executable = os.sys.executable
        params = subprocess.list2cmdline(["-m", "gupkg.gupkg", *arguments])
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, os.getcwd(), 1
        )
        return result > 32
    except Exception:
        return False


def wait_for_keypress() -> None:
    """Pause for a keypress using the Windows console when possible.

    Returns
    -------
    None
        ``None``.

    """
    try:
        import msvcrt

        msvcrt.getch()
    except ImportError:
        input("Press Enter to continue...")
