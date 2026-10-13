"""Object storage abstraction — StorageBackend + local-filesystem driver v1.

Bytes live outside Postgres behind this interface; the DB stores only the
SHA-256 digest (B6). Keys are backend-qualified (``local:…``); a future
S3-compatible driver registers under its own prefix and the swap is
mechanical (D-19).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from abc import ABC, abstractmethod


class StorageError(Exception):
    pass


class StorageBackend(ABC):
    prefix: str = ""

    @abstractmethod
    def put(self, data: bytes, *, suggested_name: str = "") -> str:
        """Store bytes; returns the backend-qualified key."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Fetch bytes by qualified key; raises StorageError when missing."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Best-effort delete; missing keys are not an error."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...


def _require_prefix(key: str, prefix: str) -> str:
    if not key.startswith(prefix + ":"):
        raise StorageError(f"key {prefix!r}-qualified key expected")
    return key[len(prefix) + 1 :]


class LocalStorageBackend(StorageBackend):
    """Local-filesystem driver. Files live under ``root`` (outside the web
    root), named by content hash + a random suffix so names are unguessable
    and content-addressed."""

    prefix = "local"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path_for(self, name: str) -> str:
        # Phase 5: per-client isolation lives in a single validated
        # subdirectory level ("<user_id>/<uuid-name>"). Each segment is
        # validated separately so "../" can never escape the root.
        parts = name.split("/")
        if len(parts) > 2:
            raise StorageError("nested directories are not allowed")
        for part in parts:
            if (
                not part
                or part.startswith(".")
                or "\\" in part
                or ".." in part
                or not all(c.isalnum() or c in "-_." for c in part)
            ):
                raise StorageError("invalid object name")
        path = os.path.abspath(os.path.join(self.root, *parts))
        if not path.startswith(self.root + os.sep):
            raise StorageError("path traversal rejected")
        if len(parts) == 2:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def put(self, data: bytes, *, suggested_name: str = "", subdir: str = "") -> str:
        import secrets as _secrets

        digest = hashlib.sha256(data).hexdigest()
        name = f"{digest[:16]}-{_secrets.token_hex(8)}"
        if suggested_name:
            safe = "".join(c for c in suggested_name if c.isalnum() or c in "._-")[:64]
            if safe:
                name = f"{name}-{safe}"
        if subdir:
            if (
                not subdir
                or "/" in subdir
                or "\\" in subdir
                or subdir.startswith(".")
                or ".." in subdir
                or not all(c.isalnum() or c in "-_" for c in subdir)
            ):
                raise StorageError("invalid subdirectory")
            name = f"{subdir}/{name}"
        path = self._path_for(name)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return f"local:{name}"

    def get(self, key: str) -> bytes:
        path = self._path_for(_require_prefix(key, "local"))
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise StorageError("object not found") from exc

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path_for(_require_prefix(key, "local")))
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path_for(_require_prefix(key, "local")))


class StorageRegistry:
    """Dispatch qualified keys to the registered backend (S3 plugs in here)."""

    def __init__(self):
        self._backends: dict[str, StorageBackend] = {}

    def register(self, backend: StorageBackend) -> None:
        self._backends[backend.prefix] = backend

    def _for(self, key: str) -> StorageBackend:
        prefix = key.split(":", 1)[0]
        try:
            return self._backends[prefix]
        except KeyError as exc:
            raise StorageError(f"no backend for prefix {prefix!r}") from exc

    def get(self, key: str) -> bytes:
        return self._for(key).get(key)

    def put(self, data: bytes, *, suggested_name: str = "", subdir: str = "") -> str:
        """Store bytes via the default ("local") backend; returns the
        backend-qualified key."""
        try:
            backend = self._backends["local"]
        except KeyError as exc:
            raise StorageError("no backend for prefix 'local'") from exc
        return backend.put(data, suggested_name=suggested_name, subdir=subdir)

    def delete(self, key: str) -> None:
        return self._for(key).delete(key)

    def exists(self, key: str) -> bool:
        return self._for(key).exists(key)
