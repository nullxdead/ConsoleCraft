"""Local offline identities. This file never stores authentication credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import UUID, uuid4

from .auth import AuthError, offline_account

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class AccountError(ValueError):
    pass


class AccountStore:
    def __init__(self, home):
        directory = home if isinstance(home, (str, os.PathLike)) else home.home
        self.home = Path(directory).expanduser().resolve()
        self.path = self.home / "accounts.json"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

    def _read(self):
        if self.path.is_symlink():
            raise AccountError("Файл аккаунтов не должен быть символической ссылкой.")
        if not self.path.exists():
            return {"schema_version": 1, "accounts": [], "active": "microsoft"}
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError
            records = data["accounts"]
            if not isinstance(records, list) or len(records) > 1000:
                raise ValueError
            clean, ids, names = [], set(), set()
            for record in records:
                identity = str(UUID(record["id"]))
                account = offline_account(record["username"])
                if identity in ids or account.username.casefold() in names:
                    raise ValueError
                ids.add(identity); names.add(account.username.casefold())
                clean.append({"id": identity, **account.public_dict()})
            active = data.get("active", "microsoft")
            if active != "microsoft" and active not in ids:
                raise ValueError
            return {"schema_version": 1, "accounts": clean, "active": active}
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, AuthError) as exc:
            raise AccountError("Файл accounts.json повреждён. Исправьте его или восстановите резервную копию.") from exc

    def _write(self, data):
        if self.path.is_symlink():
            raise AccountError("Файл аккаунтов не должен быть символической ссылкой.")
        self.home.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.home,
                                             prefix=".accounts-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        with self._lock:
            return self._read()["accounts"]

    def get(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not identifier:
            raise AccountError("Укажите ID или никнейм офлайн-аккаунта.")
        with self._lock:
            for record in self._read()["accounts"]:
                if record["id"] == identifier or record["username"].casefold() == identifier.casefold():
                    return record
        raise AccountError("Офлайн-аккаунт не найден.")

    def active(self) -> dict:
        with self._lock:
            data = self._read()
            if data["active"] == "microsoft":
                return {"kind": "microsoft"}
            return next(record for record in data["accounts"] if record["id"] == data["active"])

    def create(self, username: str, *, select: bool = True) -> dict:
        try:
            account = offline_account(username)
        except AuthError as exc:
            raise AccountError(str(exc)) from None
        with self._lock:
            data = self._read()
            if len(data["accounts"]) >= 1000:
                raise AccountError("Достигнут предел в 1000 локальных аккаунтов.")
            if any(record["username"].casefold() == username.casefold() for record in data["accounts"]):
                raise AccountError("Аккаунт с таким никнеймом уже существует.")
            record = {"id": str(uuid4()), **account.public_dict()}
            data["accounts"].append(record)
            if select: data["active"] = record["id"]
            self._write(data)
            return record

    def select(self, identifier: str) -> dict:
        with self._lock:
            data = self._read()
            record = {"kind": "microsoft"} if identifier == "microsoft" else self.get(identifier)
            data["active"] = "microsoft" if identifier == "microsoft" else record["id"]
            self._write(data)
            return record

    def update(self, identifier: str, username: str) -> dict:
        try:
            account = offline_account(username)
        except AuthError as exc:
            raise AccountError(str(exc)) from None
        with self._lock:
            data = self._read(); current = self.get(identifier)
            if any(r["id"] != current["id"] and r["username"].casefold() == username.casefold() for r in data["accounts"]):
                raise AccountError("Аккаунт с таким никнеймом уже существует.")
            record = {"id": current["id"], **account.public_dict()}
            data["accounts"] = [record if r["id"] == current["id"] else r for r in data["accounts"]]
            self._write(data)
            return record

    def remove(self, identifier: str, *, confirmed: bool = False) -> None:
        if confirmed is not True:
            raise AccountError("Подтвердите удаление офлайн-аккаунта.")
        with self._lock:
            data = self._read(); record = self.get(identifier)
            data["accounts"] = [r for r in data["accounts"] if r["id"] != record["id"]]
            if data["active"] == record["id"]: data["active"] = "microsoft"
            self._write(data)
