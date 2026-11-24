"""Windows-user-bound authorization, stored outside the repository and synced vault."""

import ctypes
import hashlib
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from app.core.errors import AuthError, ValidationError
from app.features.personal.vault import atomic_write


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Use current-user DPAPI, never machine-wide protection or interactive prompts."""
    if os.name != "nt":
        raise ValidationError("本机自动解锁仅支持 Windows。")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        success = crypt.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        )
    else:
        success = crypt.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        )
    if not success:
        raise AuthError("本机授权无法读取，请在账本界面重新授权。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class MachineAccess:
    def __init__(self, vault: Path, root: Path | None = None) -> None:
        self.vault = vault.resolve()
        if root is None:
            local = os.environ.get("LOCALAPPDATA")
            root = (Path(local) if local else Path.home()) / "BastidePersonal" / "device"
        identifier = hashlib.sha256(str(self.vault).casefold().encode()).hexdigest()
        self.path = root / (identifier + ".dpapi")

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def save(self, credential: str) -> None:
        atomic_write(self.path, protect(credential.encode("utf-8")))

    @property
    def task_name(self) -> str:
        identifier = hashlib.sha256(str(self.path).casefold().encode()).hexdigest()[:16]
        return "BastidePersonal-" + identifier

    def install_logon_task(self) -> None:
        """Start the authorized local vault after this Windows user logs in."""
        if os.name != "nt":
            raise ValidationError("开机启动仅支持 Windows。")
        script = self._registration_script()
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                _ps_encoded(script),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise ValidationError("Windows 登录启动任务注册失败，请在本机交互桌面重试。")

    def _registration_script(self) -> str:
        import sys

        backend = Path(__file__).resolve().parents[3]
        python = Path(sys.executable).with_name("pythonw.exe")
        arguments = [
            "-m",
            "app.personal_cli",
            "--no-browser",
            "--keep-running",
            "--vault",
            str(self.vault),
        ]
        action = "$action=New-ScheduledTaskAction -Execute " + _ps_quote(str(python))
        action += " -Argument " + _ps_quote(subprocess.list2cmdline(arguments))
        action += " -WorkingDirectory " + _ps_quote(str(backend))
        trigger = "Register-ScheduledTask -TaskName " + _ps_quote(self.task_name)
        trigger += " -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn)"
        trigger += " -Description 'Starts the authorized encrypted ledger after sign-in.'"
        trigger += " -Force | Out-Null"
        return "$ErrorActionPreference='Stop'; " + action + "; " + trigger

    def load(self) -> str:
        try:
            return protect(self.path.read_bytes(), decrypt=True).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise AuthError("本机授权无法读取，请在账本界面重新授权。") from exc

    def revoke(self) -> None:
        self.path.unlink(missing_ok=True)
        self.remove_logon_task()

    def remove_logon_task(self) -> None:
        if os.name != "nt":
            return
        script = "$ErrorActionPreference='Stop'; Unregister-ScheduledTask -TaskName "
        script += _ps_quote(self.task_name) + " -Confirm:$false -ErrorAction SilentlyContinue"
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                _ps_encoded(script),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )

    def logon_task_enabled(self) -> bool:
        if os.name != "nt":
            return False
        script = (
            "$task=Get-ScheduledTask -TaskName "
            + _ps_quote(self.task_name)
            + " -ErrorAction SilentlyContinue; if($task){exit 0}else{exit 1}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                _ps_encoded(script),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        return result.returncode == 0


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_encoded(value: str) -> str:
    import base64

    return base64.b64encode(value.encode("utf-16-le")).decode("ascii")
