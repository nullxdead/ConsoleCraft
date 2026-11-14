"""Saved multiplayer destinations, separate from game files and credentials."""
from __future__ import annotations
import ipaddress
import json
from pathlib import Path
import re
from uuid import uuid4
from .config import ConfigError
from .profiles import _atomic_json


class ServerStore:
    def __init__(self, home):
        self.path = Path(home) / "servers.json"

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise ConfigError("Некорректная запись сервера.")
        name, host, port = value.get("name", ""), value.get("host", ""), value.get("port", 25565)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise ConfigError("Название сервера должно содержать от 1 до 80 символов.")
        if not isinstance(host, str):
            raise ConfigError("Укажите IP или домен сервера.")
        host = host.strip().strip("[]")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                raise ConfigError("Некорректный домен сервера.") from None
            if not 1 <= len(host) <= 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", host):
                raise ConfigError("Укажите IP или домен без протокола, пути и порта.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigError("Порт должен быть от 1 до 65535.")
        identity = value.get("id", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9-]{36}", identity):
            raise ConfigError("Некорректный ID сервера.")
        return {"id": identity, "name": name.strip(), "host": host, "port": port}

    def list(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError):
            raise ConfigError("Повреждён servers.json. Исходный файл сохранён.") from None
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("servers"), list) or len(data["servers"]) > 200:
            raise ConfigError("Некорректный список серверов.")
        return [self.validate(value) for value in data["servers"]]

    def save(self, value):
        values = self.list()
        value = dict(value)
        identity = value.get("id")
        if identity and not any(item["id"] == identity for item in values):
            raise ConfigError("Сервер не найден.")
        value["id"] = identity or str(uuid4())
        value = self.validate(value)
        if not identity and len(values) >= 200:
            raise ConfigError("Сохранено слишком много серверов.")
        values = [item for item in values if item["id"] != value["id"]] + [value]
        _atomic_json(self.path, {"schema": 1, "servers": values})
        return value

    def delete(self, identity):
        values = self.list()
        remaining = [item for item in values if item["id"] != identity]
        if len(values) == len(remaining):
            raise ConfigError("Сервер не найден.")
        _atomic_json(self.path, {"schema": 1, "servers": remaining})
