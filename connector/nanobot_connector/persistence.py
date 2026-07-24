"""Durable, process-safe JSON persistence for connector-local state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock, Timeout


class LocalStateError(RuntimeError):
    """Raised when connector-local state cannot be safely read or written."""


class LocalStateConflictError(LocalStateError):
    """Raised when another process changed a registry after it was loaded."""


def _restrict_file_permissions(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        import ntsecuritycon
        import win32api
        import win32security

        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE | ntsecuritycon.DELETE,
            user_sid,
        )
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, system_sid)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, admins_sid)
        security_info = (
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION
        )
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, security_info, None, None, dacl, None
        )
    except Exception as exc:  # noqa: BLE001 - Windows security APIs expose several error types
        raise LocalStateError(f"无法设置仅当前用户可访问的 Windows DACL：{exc}") from exc


def file_permission_issue(path: Path) -> str | None:
    """Return a safe diagnostic when connector-local state is too broadly writable."""
    try:
        if os.name != "nt":
            return "本机状态文件权限过宽" if path.stat().st_mode & 0o077 else None
        import ntsecuritycon
        import win32api
        import win32security

        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        admins_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        trusted_sids = {str(user_sid), str(system_sid), str(admins_sid)}
        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return "Windows DACL 缺失，无法保护本机状态文件"
        write_mask = (
            ntsecuritycon.FILE_GENERIC_WRITE
            | ntsecuritycon.FILE_WRITE_DATA
            | ntsecuritycon.FILE_APPEND_DATA
            | ntsecuritycon.WRITE_DAC
            | ntsecuritycon.DELETE
        )
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE and ace[1] & write_mask:
                if str(ace[2]) not in trusted_sids:
                    return "Windows DACL 向非受信主体授予了本机状态写权限"
    except Exception as exc:  # noqa: BLE001 - diagnostics must stay readable
        return f"无法验证 Windows DACL：{exc}"
    return None


def file_fingerprint(path: Path) -> str | None:
    """Return a content fingerprint, including a stable marker for a missing file."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocalStateError(f"无法读取本机状态文件 {path}: {exc}") from exc


@contextmanager
def locked_file(path: Path, *, timeout: float = 10) -> Iterator[None]:
    """Hold a target-specific cross-process lock for a short local mutation."""
    try:
        with FileLock(f"{path}.lock", timeout=timeout):
            yield
    except Timeout as exc:
        raise LocalStateError(f"等待本机状态写入锁超时：{path}") from exc


def write_json_atomic(path: Path, data: Any, *, mode: int = 0o600) -> None:
    """Durably replace a JSON document without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_file_permissions(tmp, mode)
            os.replace(tmp, path)
            if os.name != "nt":
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                except OSError:
                    directory_fd = None
                if directory_fd is not None:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        except OSError as exc:
            raise LocalStateError(f"无法原子写入本机状态文件 {path}: {exc}") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
