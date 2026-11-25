import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def integer(value, minimum, maximum):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        result = int(value)
    else:
        raise ValueError("Укажи целое число.")
    if not minimum <= result <= maximum:
        raise ValueError(f"Число должно быть от {minimum} до {maximum}.")
    return result


def https_base(value):
    url = urlsplit(value)
    if (
        url.scheme != "https" or not url.hostname or url.username is not None
        or url.query or url.fragment or any(c.isspace() for c in value)
        or "\\" in value
    ):
        raise ValueError("Укажи HTTPS-адрес без логина, пароля, запроса и фрагмента.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Node:
    key: str
    label: str
    api_url: str
    subscription_url: str
    inbound_id: int
    token: str = field(repr=False)
    flow: str = ""

    @classmethod
    def read(cls, data):
        key = data["key"]
        label = data["label"].strip()
        token = data["token"].strip()
        flow = data.get("flow", "")
        if not re.fullmatch(r"[a-z0-9_-]{1,24}", key):
            raise ValueError("Ключ ноды должен содержать до 24 латинских букв, цифр, - или _.")
        if not label or len(label) > 40 or not label.isprintable():
            raise ValueError("Название ноды должно содержать от 1 до 40 символов.")
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("Укажи Bearer-токен панели без пробелов.")
        if flow not in ("", "xtls-rprx-vision"):
            raise ValueError("Укажи пустой flow или xtls-rprx-vision.")
        return cls(
            key, label, https_base(data["api_url"]), https_base(data["subscription_url"]),
            integer(data["inbound_id"], 1, 2**31 - 1), token, flow,
        )


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    admins: frozenset[int]
    brand: str
    welcome: str
    support: str
    terms_url: str
    test_mode: bool
    price: int
    trial_days: int
    nodes: dict[str, Node]
    database: Path
    openai_key: str = field(repr=False)
    openai_model: str

    @classmethod
    def from_env(cls):
        env = os.environ
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажи TELEGRAM_BOT_TOKEN от BotFather.")
        support = env.get("SUPPORT_USERNAME", "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", support):
            raise ValueError("Укажи Telegram-имя поддержки в SUPPORT_USERNAME.")
        admins = frozenset(
            integer(part.strip(), 1, 2**52 - 1)
            for part in env.get("ADMIN_IDS", "").split(",")
        )
        data = json.loads(Path(env.get("NODES_FILE", "nodes.json")).read_text())
        if not isinstance(data, list) or not 1 <= len(data) <= 20:
            raise ValueError("В nodes.json должна быть от 1 до 20 нод.")
        nodes = {node.key: node for node in map(Node.read, data)}
        if len(nodes) != len(data):
            raise ValueError("Ключи нод должны различаться.")
        locations = {(n.api_url, n.inbound_id) for n in nodes.values()}
        if len(locations) != len(nodes):
            raise ValueError("Каждая нода должна указывать на отдельный inbound.")
        brand = env.get("BOT_BRAND", "RenderrVPN").strip()
        welcome = env.get("BOT_WELCOME", "").strip()
        if not 1 <= len(brand) <= 36 or not brand.isprintable() or len(welcome) > 1000:
            raise ValueError("Название бота должно быть до 36 символов, приветствие до 1000.")
        key = env.get("OPENAI_API_KEY", "").strip()
        model = env.get("OPENAI_MODEL", "").strip()
        if key and not model:
            raise ValueError("С OPENAI_API_KEY укажи OPENAI_MODEL.")
        return cls(
            token, admins, brand, welcome, support, https_base(env.get("TERMS_URL", "")),
            bool(integer(env.get("TELEGRAM_TEST_MODE", "0"), 0, 1)),
            integer(env.get("PRICE_STARS", "150"), 1, 10_000),
            integer(env.get("TRIAL_DAYS", "0"), 0, 30),
            nodes, Path(env.get("DATABASE_PATH", "data/renderrvpn.sqlite3")), key, model,
        )
