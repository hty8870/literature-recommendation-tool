from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialError(ValueError):
    pass


def private_write(path: Path, data: bytes, *, create_only: bool = False) -> None:
    with NamedTemporaryFile(dir=path.parent, prefix=".credential-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            if create_only:
                os.link(temporary, path)
            else:
                temporary.replace(path)
        finally:
            file.close()
            temporary.unlink(missing_ok=True)


class CredentialVault:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path

    def _key(self, *, create: bool = False) -> bytes:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            try:
                descriptor = os.open(self.key_path, flags)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    private_write(self.key_path, secrets.token_bytes(32), create_only=True)
                except FileExistsError:
                    pass  # Another first save already initialized the key.
                descriptor = os.open(self.key_path, flags)
            with os.fdopen(descriptor, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
                    raise CredentialError("加密密钥必须是仅服务账号可读的文件。")
                value = file.read(33)
        except OSError:
            raise CredentialError("无法读取凭据加密密钥。") from None
        if len(value) != 32:
            raise CredentialError("凭据加密密钥必须为 32 字节。")
        return value

    def seal(self, plaintext: bytes, context: bytes, *, create_key: bool = False) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self._key(create=create_key)).encrypt(nonce, plaintext, context)

    def open(self, ciphertext: bytes, context: bytes) -> bytes:
        try:
            return AESGCM(self._key()).decrypt(ciphertext[:12], ciphertext[12:], context)
        except (InvalidTag, ValueError) as exc:
            if isinstance(exc, CredentialError):
                raise
            raise CredentialError("保存的凭据无法解密。") from None
