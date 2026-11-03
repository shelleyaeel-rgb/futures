"""Local credentials encrypted for the current Windows user with DPAPI."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


SECRET_NAMES = frozenset({"TQ_USERNAME", "TQ_PASSWORD", "OPENAI_API_KEY"})
DEFAULT_SECRET_PATH = Path(__file__).resolve().parents[1] / ".local" / "secrets.dpapi"
_ENTROPY = b"futures-probe-credentials-v1"
_MAX_FILE_BYTES = 64 * 1024


class CredentialError(RuntimeError):
    """Safe error message that never includes credential contents."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise CredentialError("凭据加密存储仅支持 Windows。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    target = _DataBlob()
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                              ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        description = None
    else:
        operation = crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                              ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        description = "Local futures probe credentials"
    operation.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN: never trigger an authentication dialog.
    success = operation(ctypes.byref(source), description, ctypes.byref(entropy),
                        None, None, 1, ctypes.byref(target))
    # Keep both source buffers alive until the native call has completed.
    _ = (source_buffer, entropy_buffer)
    if not success:
        raise CredentialError("无法读取本机加密凭据，请使用当前 Windows 账户重新配置。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        if target.pbData:
            ctypes.memset(target.pbData, 0, target.cbData)
            kernel32.LocalFree(target.pbData)


def load_secrets(path: Path | str | None = None) -> dict[str, str]:
    """Read the encrypted store, returning an empty dict when it does not exist."""
    target = Path(path) if path is not None else DEFAULT_SECRET_PATH
    if not target.exists():
        return {}
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise CredentialError("本机凭据文件格式无效，请重新配置。")
        values = json.loads(_crypt(target.read_bytes(), decrypt=True).decode("utf-8"))
        if not isinstance(values, dict) or any(
            name not in SECRET_NAMES or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise CredentialError("本机凭据文件格式无效，请重新配置。")
        return values
    except CredentialError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise CredentialError("无法读取本机凭据文件，请重新配置。") from None


def save_secrets(values: dict[str, str], path: Path | str | None = None) -> None:
    """Merge and atomically save only the allowed nonempty credential fields."""
    if os.name != "nt":
        raise CredentialError("凭据加密存储仅支持 Windows。")
    if not values or any(
        name not in SECRET_NAMES or not isinstance(value, str) or not value or len(value) > 8192
        for name, value in values.items()
    ):
        raise CredentialError("凭据字段不完整或格式无效。")
    target = Path(path) if path is not None else DEFAULT_SECRET_PATH
    current = load_secrets(target)
    current.update(values)
    payload = json.dumps(current, ensure_ascii=False).encode("utf-8")
    if len(payload) > 48 * 1024:
        raise CredentialError("凭据内容过长。")
    encrypted = _crypt(payload, decrypt=False)
    temporary: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".credentials-", delete=False) as stream:
            temporary = stream.name
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError:
        raise CredentialError("无法保存本机加密凭据。") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def get_secret(name: str, path: Path | str | None = None) -> str | None:
    """Environment values take precedence over the local encrypted store."""
    if name not in SECRET_NAMES:
        raise CredentialError("不支持的凭据字段。")
    return os.environ.get(name) or load_secrets(path).get(name)


def credential_status(path: Path | str | None = None) -> dict[str, bool]:
    """Return presence flags, never values."""
    stored = load_secrets(path)
    return {name: bool(os.environ.get(name) or stored.get(name)) for name in sorted(SECRET_NAMES)}
