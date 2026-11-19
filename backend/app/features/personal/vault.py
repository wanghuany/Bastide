"""Authenticated encrypted persistence; SQLite plaintext exists only in process memory.

This is an encrypted-container design, NOT SQLCipher. Every acknowledged mutation has
already been serialized, AES-GCM encrypted, fsynced and atomically replaced on disk.
One process owns the vault; one reentrant lock serializes all readers and writers.
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import struct
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.errors import AuthError, ConflictError, ValidationError
from app.core.schema import alembic_config

MAGIC = b"BASTIDE-PERSONAL\x01\n"
MAX_VAULT_BYTES = 128 * 1024 * 1024
MAX_HEADER_BYTES = 8192


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _password_key(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(password.encode("utf-8"), salt, 3, 65536, 4, 32, Type.ID)


def _recovery_key(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value.replace("-", "").replace(" ", ""))
    except ValueError as exc:
        raise AuthError("恢复密钥格式不正确。") from exc
    if len(raw) != 32:
        raise AuthError("恢复密钥格式不正确。")
    return hashlib.sha256(b"bastide-personal-recovery\x00" + raw).digest()


def _wrap(key: bytes, data_key: bytes, label: bytes) -> str:
    nonce = secrets.token_bytes(12)
    return _b64(nonce + AESGCM(key).encrypt(nonce, data_key, MAGIC + label))


def _unwrap(key: bytes, wrapped: str, label: bytes) -> bytes:
    raw = _unb64(wrapped)
    return AESGCM(key).decrypt(raw[:12], raw[12:], MAGIC + label)


def atomic_write(path: Path, data: bytes) -> None:
    """Write ciphertext only, preserving the last intact file if replacement fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Vault:
    """Single-writer vault with password/recovery unlock and encrypted rollback safety."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mutex = threading.RLock()
        self.connection: sqlite3.Connection | None = None
        self.engine: Engine | None = None
        self.header: dict[str, Any] = {}
        self.data_key: bytes | None = None
        self._lock_stream: Any = None
        self._disk_digest: bytes | None = None

    @property
    def unlocked(self) -> bool:
        return self.connection is not None

    def _claim(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_suffix(".lock").open("a+b")
        try:
            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise ConflictError("该账本已被另一个进程打开。") from exc
        self._lock_stream = stream

    def _release(self) -> None:
        if self._lock_stream is not None:
            self._lock_stream.close()
            self._lock_stream = None

    def _open_memory(self, raw: bytes | None = None) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        if raw is not None:
            connection.deserialize(raw)
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA journal_mode=MEMORY")
        self.connection = connection
        self.engine = create_engine(
            "sqlite://", creator=lambda: connection, poolclass=StaticPool, hide_parameters=True
        )
        config = alembic_config(Path(":memory:"))
        with self.engine.begin() as supplied:
            config.attributes["connection"] = supplied
            command.upgrade(config, "head")
        connection.execute("PRAGMA foreign_keys=ON")
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise ValidationError("账本完整性检查失败。")

    def create(self, password: str) -> str:
        """Create an empty encrypted vault; return its one-time recovery key."""
        with self.mutex:
            if self.path.exists() or self.unlocked:
                raise ConflictError("账本已存在，请解锁。")
            if len(password) < 10 or len(password) > 1024:
                raise ValidationError("主密码需要 10 至 1024 个字符。")
            self._claim()
            try:
                salt = secrets.token_bytes(16)
                self.data_key = secrets.token_bytes(32)
                recovery = secrets.token_bytes(32).hex().upper()
                recovery = "-".join(recovery[i : i + 4] for i in range(0, 64, 4))
                self.header = {
                    "format": 1,
                    "kdf": "argon2id-3-65536-4",
                    "salt": _b64(salt),
                    "password_wrap": _wrap(
                        _password_key(password, salt), self.data_key, b"password"
                    ),
                    "recovery_wrap": _wrap(_recovery_key(recovery), self.data_key, b"recovery"),
                }
                self._open_memory()
                self._save()
                return recovery
            except Exception:
                self.lock()
                raise

    @staticmethod
    def decode(blob: bytes, credential: str, recovery: bool = False) -> tuple[dict, bytes, bytes]:
        """Authenticate the entire encrypted envelope before SQLite sees any bytes."""
        try:
            if len(blob) > MAX_VAULT_BYTES or not blob.startswith(MAGIC):
                raise ValueError
            offset = len(MAGIC)
            size = struct.unpack(">I", blob[offset : offset + 4])[0]
            if not 0 < size <= MAX_HEADER_BYTES:
                raise ValueError
            start = offset + 4
            encoded_header = blob[start : start + size]
            header = json.loads(encoded_header)
            if header["format"] != 1 or header["kdf"] != "argon2id-3-65536-4":
                raise ValueError
            salt = _unb64(header["salt"])
            if len(salt) != 16:
                raise ValueError
            key = _recovery_key(credential) if recovery else _password_key(credential, salt)
            label = b"recovery" if recovery else b"password"
            data_key = _unwrap(key, header[label.decode() + "_wrap"], label)
            ciphertext = blob[start + size :]
            raw = AESGCM(data_key).decrypt(ciphertext[:12], ciphertext[12:], MAGIC + encoded_header)
            if not raw.startswith(b"SQLite format 3\x00"):
                raise ValueError
            return header, data_key, raw
        except (ValueError, KeyError, TypeError, InvalidTag, struct.error) as exc:
            raise AuthError("密码/恢复密钥不正确，或加密文件已损坏。") from exc

    def unlock(self, credential: str, recovery: bool = False) -> None:
        with self.mutex:
            if self.unlocked:
                raise ConflictError("账本已解锁。")
            if not self.path.exists():
                raise ValidationError("请先创建账本或恢复备份。")
            self._claim()
            try:
                encrypted = self.path.read_bytes()
                self.header, self.data_key, raw = self.decode(encrypted, credential, recovery)
                self._disk_digest = hashlib.sha256(encrypted).digest()
                self._open_memory(raw)
                self._save()
            except Exception:
                self.lock()
                raise

    def _save(self) -> None:
        if self.connection is None or self.data_key is None:
            raise AuthError("请先解锁账本。")
        header = json.dumps(self.header, separators=(",", ":"), sort_keys=True).encode()
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self.data_key).encrypt(
            nonce, self.connection.serialize(), MAGIC + header
        )
        blob = MAGIC + struct.pack(">I", len(header)) + header + nonce + encrypted
        if len(blob) > MAX_VAULT_BYTES:
            raise ValidationError("账本超过本版本 128 MiB 的上限，请先归档历史数据。")
        # A sync client can replace a file without respecting our local process lock.
        # Refuse to overwrite a newer/deleted snapshot. This is a last-line conflict
        # check, not a distributed lock: never edit from two computers concurrently.
        current = hashlib.sha256(self.path.read_bytes()).digest() if self.path.exists() else None
        if current != self._disk_digest:
            raise ConflictError(
                "账本文件已被同步或外部程序更改。请停止写入，锁定后等待同步完成再解锁。"
            )
        atomic_write(self.path, blob)
        self._disk_digest = hashlib.sha256(blob).digest()

    @contextmanager
    def session(self, write: bool = False) -> Generator[Session]:
        """Serialize operations; roll memory back too if an encrypted disk commit fails."""
        with self.mutex:
            if self.engine is None or self.connection is None:
                raise AuthError("请先在本地界面解锁账本。")
            previous = self.connection.serialize() if write else None
            with Session(self.engine, expire_on_commit=False) as session:
                try:
                    yield session
                    if write:
                        session.commit()
                        self._save()
                    else:
                        session.rollback()
                except Exception:
                    session.rollback()
                    if previous is not None:
                        self.connection.deserialize(previous)
                        self.connection.execute("PRAGMA foreign_keys=ON")
                    raise

    def change_password(self, password: str) -> None:
        with self.mutex:
            if self.data_key is None or not self.unlocked:
                raise AuthError("请先解锁账本。")
            if not 10 <= len(password) <= 1024:
                raise ValidationError("主密码需要 10 至 1024 个字符。")
            previous = dict(self.header)
            try:
                salt = secrets.token_bytes(16)
                self.header = {
                    **self.header,
                    "salt": _b64(salt),
                    "password_wrap": _wrap(
                        _password_key(password, salt), self.data_key, b"password"
                    ),
                }
                self._save()
            except Exception:
                self.header = previous
                raise

    def backup(self) -> bytes:
        with self.mutex:
            if not self.unlocked:
                raise AuthError("请先解锁账本。")
            return self.path.read_bytes()

    def restore(self, blob: bytes, credential: str, recovery: bool = False) -> None:
        """Restore only while locked; validate before replacing and preserve old ciphertext."""
        with self.mutex:
            if self.unlocked:
                raise ConflictError("恢复前请先锁定账本。")
            self._claim()
            try:
                header, key, raw = self.decode(blob, credential, recovery)
                self.header, self.data_key = header, key
                self._open_memory(raw)
                if self.path.exists():
                    old = self.path.with_name(self.path.stem + ".before-restore.bfin")
                    previous = self.path.read_bytes()
                    self._disk_digest = hashlib.sha256(previous).digest()
                    atomic_write(old, previous)
                self._save()
            finally:
                self.lock()

    def lock(self) -> None:
        with self.mutex:
            if self.engine is not None:
                self.engine.dispose()
            self.connection = None
            self.engine = None
            self.header = {}
            self.data_key = None
            self._disk_digest = None
            self._release()
