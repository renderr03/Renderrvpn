import secrets
import time
from dataclasses import asdict, dataclass, field
from threading import RLock

from .config import Node, https_base, integer, panel_base, subscription_origin, support_username
from .services import Panel, ServiceError


USER_ID_MAX = 2**52 - 1
SETTINGS = {
    "brand": ("Название магазина", "Отправь название магазина. До 36 символов."),
    "welcome": ("Приветствие", "Отправь приветствие. До 1000 символов."),
    "support": ("Контакт поддержки", "Отправь Telegram-имя поддержки. Например, @vpn_support."),
    "terms_url": ("Условия покупки", "Отправь HTTPS-ссылку на условия покупки."),
    "unified_url": ("Единая подписка", "Отправь HTTPS-домен обратного прокси для единой подписки. Пример https://sub.example.com."),
    "openai_key": ("Ключ OpenAI", "Отправь ключ OpenAI. Бот попробует удалить сообщение с ключом."),
    "openai_model": ("Модель OpenAI", "Отправь название модели с поддержкой Structured Outputs."),
}


def button(title, action):
    return {"text": title, "callback_data": action}


def back(target="admin"):
    return [[button("‹ Назад", target)]]


@dataclass
class Draft:
    step: str
    parent: str
    data: dict = field(default_factory=dict)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(7))
    expires: float = field(default_factory=lambda: time.monotonic() + 900)


class AdminMenu:
    def __init__(self, bot):
        self.bot = bot
        self.drafts = {}
        self.lock = RLock()

    def leave(self, user):
        with self.lock:
            self.drafts.pop(user, None)

    def page(self, user, text, rows=None, parent="admin"):
        self.bot.send(user, text, (rows or []) + back(parent))

    def draft(self, user, step, parent, data=None):
        value = Draft(step, parent, data or {})
        with self.lock:
            self.drafts[user] = value
        return value

    def current(self, user):
        with self.lock:
            value = self.drafts.get(user)
            if value is not None and time.monotonic() >= value.expires:
                self.leave(user)
                return None
            return value

    def prompt(self, user, step, text, parent, data=None, choices=()):
        draft = self.draft(user, step, parent, data)
        rows = [[button(label, f"w:{draft.nonce}:{choice}")] for label, choice in choices]
        rows.append([button("Отмена", parent)])
        self.bot.send(user, text, rows)
        return draft

    def home(self, user):
        self.leave(user)
        rows = []
        if not self.bot.nodes():
            rows.append([button("Добавить основную панель 3x-ui", "a:addnode")])
        rows.extend([
            [button("Клиенты", "a:clients"), button("Тарифы", "a:tariffs")],
            [button("Панели и ноды", "a:nodes"), button("Платежи", "a:payments")],
            [button("ИИ-помощник", "a:ai"), button("Настройки", "a:settings")],
            [button("Журнал", "a:audit")],
        ])
        self.page(user, "Управление VPN-магазином", rows, "home")

    def servers(self, user):
        nodes = self.bot.nodes()
        rows = [[button(node.label, f"a:node:{node.key}")] for node in nodes.values()]
        if len(nodes) < 20:
            title = "Добавить ноду" if nodes else "Добавить основную панель 3x-ui"
            rows.append([button(title, "a:addnode")])
        if nodes:
            rows.append([button("Проверить все ноды", "a:health")])
        self.page(user, "Панели и ноды" if nodes else "Подключи первую панель 3x-ui.", rows)

    def node(self, user, key):
        node = self.bot.nodes().get(key)
        if not node:
            raise ValueError("Локация не найдена.")
        opened = self.bot.store.sales_open(key)
        health = self.bot.store.node_health(key)
        detail = health["detail"] if health else "проверка ещё не выполнялась"
        self.page(user, f"{node.label}\nInbound {node.inbound_id}\nНовые продажи {'открыты' if opened else 'закрыты'}.\nМониторинг · {detail}.",
                  [[button("Проверить состояние", f"a:health:{key}")],
                   [button("Закрыть новые продажи" if opened else "Открыть новые продажи",
                           f"a:sales:{key}:{int(not opened)}")],
                   [button("Обновить токен панели", f"a:rotate:{key}")]], "a:nodes")

    def client(self, user, target, text, exists):
        rows = [[button("Выдать или продлить", f"a:grant:{target}")]]
        if exists:
            rows.append([button("Отключить доступ", f"a:revoke:{target}")])
            rows.append([button("Повторить синхронизацию", f"a:retry:{target}")])
        self.page(user, text, rows, "a:clients")

    def payments(self, user, db):
        rows = db.db.execute("SELECT rowid AS number, * FROM payments ORDER BY paid_at DESC, number DESC LIMIT 12")
        output = []
        keyboard = []
        statuses = {"paid": "оплачен", "refund_pending": "возврат в очереди", "refunded": "возвращён"}
        for row in rows:
            number = row["number"]
            output.append(f"#{number} · ID {row['user_id']} · {row['amount']} ⭐ · {statuses[row['refund_state']]}"
                          + (f" · {row['last_error']}" if row["last_error"] else ""))
            if row["refund_state"] == "paid":
                keyboard.append([button(f"Вернуть платёж #{number}", f"a:refund:{number}")])
        if output:
            keyboard.append([button("Найти платёж по номеру", "a:findpayment")])
        self.page(user, "\n".join(output) or "Платежей пока нет.", keyboard)

    def settings(self, user):
        rows = [[button(title, f"a:setting:{key}")] for key, (title, _) in SETTINGS.items()]
        if self.bot.setting("welcome"):
            rows.append([button("Очистить приветствие", "a:clear:welcome")])
        if self.bot.setting("openai_key"):
            rows.append([button("Выключить ИИ-помощника", "a:clear:openai_key")])
        unified = self.bot.store.setting("unified_url", "")
        if unified:
            rows.append([button("Отключить единую подписку", "a:unified_off")])
        brand = self.bot.setting("brand")
        support_status = "указан" if self.bot.setting("support") else "нужно указать"
        terms_status = "указана" if self.bot.setting("terms_url") else "нужно указать"
        status = unified if unified else "выключена"
        self.page(user, f"Магазин {brand}\nКонтакт поддержки {support_status}.\nСсылка на условия {terms_status}.\nЕдиная подписка · {status}", rows)

    def add_node(self, user):
        panels = {node.api_url: node for node in self.bot.nodes().values()}
        if panels:
            rows = [[button(f"Из панели · {node.label}", f"a:reuse:{node.key}")]
                    for node in panels.values()]
            rows.append([button("Подключить другую панель", "a:newpanel")])
            self.page(user, "Выбери панель для новой ноды.", rows, "a:nodes")
        else:
            self.panel_address(user)

    def panel_address(self, user):
        self.prompt(user, "api", "Отправь адрес панели 3x-ui. Если панель на этом же VPS, можно использовать http://127.0.0.1 с портом и базовым путём.", "a:nodes")

    def erase_secret(self, user, message_id):
        try:
            self.bot.telegram.call("deleteMessage", chat_id=user, message_id=message_id)
        except ServiceError:
            self.bot.send(user, "Telegram не удалил сообщение. Удали его в чате вручную.")

    def probe(self, user, draft, rotation=False):
        def load(_db):
            data = draft.data
            if rotation:
                node = Node.read(data)
                okay, status = Panel(node).health()
                if not okay:
                    raise ValueError(status)
                if self.current(user) is draft:
                    self.summary(user, data, rotation=True)
                return
            # The inbound and subscription URL are selected after discovery.
            probe = Node.read(dict(data, key="probe", label="3x-ui", inbound_id=1,
                                  subscription_url="https://example.invalid"))
            options, uri = Panel(probe).catalog()
            registered = {n.inbound_id for n in self.bot.nodes(_db).values() if n.api_url == probe.api_url}
            options = [item for item in options if item["id"] not in registered]
            if not options:
                raise ValueError("На панели нет свободных VLESS-inbound для подключения.")
            try:
                uri = https_base(uri) if isinstance(uri, str) and uri else ""
            except ValueError:
                uri = ""
            if self.current(user) is draft:
                self.inbounds(user, dict(data, options=options, suggested=uri), 0)

        def failed(message):
            if self.current(user) is draft:
                self.leave(user)
                self.page(user, message, [[button("Попробовать снова", draft.parent if rotation else "a:addnode")]], "a:nodes")

        self.page(user, "Проверяю доступ к 3x-ui…", parent=draft.parent)
        if not self.bot.admin_task(user, load, failed):
            self.leave(user)

    def inbounds(self, user, data, page):
        limit = 8
        options = data["options"]
        rows = [(f"{str(item.get('remark') or 'VLESS')[:37]} · #{item['id']}", f"pick:{index}")
                for index, item in enumerate(options[page:page + limit], page)]
        if page > 0:
            rows.append(("Предыдущие", f"page:{page - limit}"))
        if page + limit < len(options):
            rows.append(("Следующие", f"page:{page + limit}"))
        self.prompt(user, "inbound", "Выбери VLESS-inbound из панели.", "a:nodes", data, rows)

    def summary(self, user, data, rotation=False):
        if rotation:
            self.prompt(user, "save", f"Обновить токен ноды {data['label']}?",
                        f"a:node:{data['key']}", dict(data, rotate=True), (("Сохранить", "save"),))
            return
        flow = "XTLS Vision" if data.get("flow") else "обычный VLESS"
        options = [("Подключить ноду", "save"), ("Изменить название", "label"),
                   ("Изменить ссылку подписок", "sub")]
        if data.get("vision"):
            options.append(("Переключить режим VLESS", "flow"))
        self.prompt(user, "save", f"{data['label']} · inbound {data['inbound_id']}\n{flow}\n"
                    f"Ссылка подписок {data['subscription_url']}", "a:nodes", data, options)

    def check_and_save(self, user, draft):
        data = draft.data
        rotate = data.get("rotate", False)
        node = Node.read(data)

        def finish(db):
            healthy, status = Panel(node).health() if not rotate else (True, "")
            if not healthy:
                raise ValueError(status)
            with self.lock:
                if self.current(user) is not draft:
                    return
                db.save_node(node, self.bot.config.nodes, user, rotate=rotate)
                self.leave(user)
            rows = [[button("Открыть ноду", f"a:node:{node.key}")]]
            if not self.bot.setting("terms_url", db) or not self.bot.setting("support", db):
                rows.append([button("Настроить условия и поддержку", "a:settings")])
            self.page(user, f"Токен обновлён для {node.label}." if rotate else f"Нода {node.label} подключена.",
                      rows, "a:nodes")

        def failed(message):
            if self.current(user) is draft:
                self.leave(user)
                self.page(user, message, parent="a:nodes")

        draft = self.draft(user, "saving", draft.parent, data)
        self.page(user, "Сохраняю ноду после проверки…", parent=draft.parent)
        if not self.bot.admin_task(user, finish, failed):
            self.summary(user, data, rotation=rotate)

    def choose(self, user, nonce, choice):
        draft = self.current(user)
        if not draft or draft.nonce != nonce:
            self.page(user, "Эта кнопка устарела. Открой нужный раздел снова.")
            return
        data = draft.data
        if draft.step == "inbound":
            kind, _, index = choice.partition(":")
            index = integer(index, 0, len(data["options"]) - 1)
            if kind == "page" and index % 8 == 0:
                self.inbounds(user, data, index)
            elif kind == "pick":
                item = data["options"][index]
                raw_label = str(item.get("remark") or "")
                label = "".join(c for c in raw_label if c.isprintable()).strip()[:40] or f"Локация {item['id']}"
                vision = item.get("tlsFlowCapable") is True
                values = {k: data[k] for k in ("api_url", "token")}
                values.update(key=secrets.token_hex(5), inbound_id=item["id"], label=label,
                              vision=vision, flow="xtls-rprx-vision" if vision else "")
                if data["suggested"]:
                    self.summary(user, dict(values, subscription_url=data["suggested"]))
                else:
                    self.prompt(user, "sub", "Отправь HTTPS-адрес сервера подписок вместе с путём. Пример https://vpn.example.com/sub", "a:nodes", values)
        elif draft.step == "save":
            if choice == "save":
                self.check_and_save(user, draft)
            elif choice == "flow" and data.get("vision") and not data.get("rotate"):
                self.summary(user, dict(data, flow="" if data["flow"] else "xtls-rprx-vision"))
            elif choice in ("label", "sub") and not data.get("rotate"):
                text = "Отправь название локации. До 40 символов." if choice == "label" else "Отправь HTTPS-адрес сервера подписок вместе с путём."
                self.prompt(user, choice, text, "a:nodes", data)
        elif draft.step == "days":
            if choice in {"7", "30", "90"}:
                self.grant(user, data["user"], int(choice))
        elif draft.step == "grant_location":
            self.leave(user)
            self.bot.admin_action(user, "grant", dict(data, node=choice), self.bot.store)
        elif draft.step == "unified_off" and choice == "confirm":
            self.save_setting(user, "unified_url", "")
            self.settings(user)
        elif draft.step == "unified_on" and choice == "confirm":
            self.save_setting(user, "unified_url", data["url"])
            self.settings(user)
        else:
            self.page(user, "Выбери действие в меню.")

    def grant(self, user, target, days):
        access = self.bot.store.access(target)
        nodes = self.bot.nodes()
        if access or len(nodes) == 1:
            self.leave(user)
            self.bot.admin_action(user, "grant", {"user": target, "days": days}, self.bot.store)
        elif nodes:
            self.prompt(user, "grant_location", "Выбери локацию для клиента.", f"a:client:{target}",
                        {"user": target, "days": days}, [(n.label, n.key) for n in nodes.values()])
        else:
            self.page(user, "Сначала подключи панель 3x-ui.", [[button("Добавить панель", "a:addnode")]])

    def message(self, user, text, message_id):
        draft = self.current(user)
        if not draft:
            self.home(user)
            return
        step, data = draft.step, draft.data
        if step == "api":
            base = panel_base(text)
            self.prompt(user, "token", "Отправь Bearer-токен API панели 3x-ui с правами admin. Бот попробует удалить сообщение с токеном.",
                        draft.parent, {"api_url": base})
        elif step in ("token", "rotation"):
            self.erase_secret(user, message_id)
            if not text or len(text) > 512 or not text.isascii() or not text.isprintable() or " " in text:
                raise ValueError("Токен должен быть одной строкой без пробелов.")
            candidate = self.draft(user, "checking", draft.parent, dict(data, token=text))
            self.probe(user, candidate, step == "rotation")
        elif step in ("sub", "label"):
            if step == "sub":
                data = dict(data, subscription_url=https_base(text))
            else:
                if not text or len(text) > 40 or not text.isprintable():
                    raise ValueError("Название должно содержать от 1 до 40 символов.")
                data = dict(data, label=text)
            self.summary(user, data)
        elif step == "setting":
            name = data["name"]
            if name == "support":
                text = support_username(text)
            elif name == "terms_url":
                text = https_base(text)
            elif name == "unified_url":
                if not self.bot.unified_available:
                    raise ValueError("Локальный порт единой подписки недоступен. Проверь настройки запуска на VPS.")
                text = subscription_origin(text)
                self.prompt(user, "unified_on",
                            f"Включить единую подписку на {text}? Бот начнёт выдавать действующим клиентам профили на открытых нодах.",
                            "a:settings", {"url": text}, (("Подтвердить", "confirm"),))
                return
            elif name == "openai_key":
                self.erase_secret(user, message_id)
                if not text or len(text) > 512 or not text.isascii() or not text.isprintable() or " " in text:
                    raise ValueError("Ключ должен быть одной строкой без пробелов.")
            elif name in ("brand", "openai_model"):
                if not text or len(text) > (36 if name == "brand" else 100) or not text.isprintable():
                    raise ValueError("Проверь длину и укажи значение в одной строке.")
            elif len(text) > 1000:
                raise ValueError("Приветствие должно содержать до 1000 символов.")
            self.save_setting(user, name, text)
            self.page(user, "Настройка сохранена.", parent=draft.parent)
        elif step in ("price", "trial", "client", "refund"):
            field_name = {"price": "amount", "trial": "days", "client": "user", "refund": "payment"}[step]
            self.bot.admin_action(user, "find" if step == "client" else step, {field_name: text}, self.bot.store)
            self.leave(user)
        elif step == "days":
            days = integer(text, 1, 366)
            self.grant(user, data["user"], days)
        elif step == "ai":
            if not text or len(text) > 1500:
                raise ValueError("Отправь задачу длиной до 1500 символов.")
            self.leave(user)
            self.bot.background_admin(user, "assistant", {"message": text})
        else:
            self.page(user, "Продолжи через кнопки на предыдущем экране.", parent=draft.parent)

    def save_setting(self, user, name, text):
        self.leave(user)
        with self.bot.store.write():
            self.bot.store.set_setting(name, text)
            self.bot.store.log_action(user, f"настройка {name}", {})

    def handle_button(self, user, command):
        action, _, key = command.partition(":")
        self.leave(user)
        if action == "nodes":
            self.servers(user)
        elif action == "node":
            self.node(user, key)
        elif action == "addnode":
            self.add_node(user)
        elif action == "newpanel":
            self.panel_address(user)
        elif action == "reuse":
            node = self.bot.nodes().get(key)
            if not node:
                raise ValueError("Панель не найдена.")
            pending = self.draft(user, "checking", "a:nodes", {"api_url": node.api_url, "token": node.token})
            self.probe(user, pending)
        elif action == "rotate":
            node = self.bot.nodes().get(key)
            if not node:
                raise ValueError("Локация не найдена.")
            self.prompt(user, "rotation", "Отправь новый Bearer-токен панели 3x-ui. Бот попробует удалить сообщение с токеном.",
                        f"a:node:{key}", asdict(node))
        elif action == "health":
            self.bot.background_admin(user, "nodes", {"node": key or None})
        elif action == "clients":
            self.prompt(user, "client", "Отправь числовой Telegram ID клиента. Клиент может скопировать его кнопкой «Мой Telegram ID».", "admin")
        elif action in ("client", "revoke", "retry"):
            self.bot.admin_action(user, "find" if action == "client" else action, {"user": key}, self.bot.store)
        elif action == "grant":
            target = integer(key, 1, USER_ID_MAX)
            self.prompt(user, "days", "Выбери срок или отправь число дней от 1 до 366.", f"a:client:{target}",
                        {"user": target}, (("7 дней", "7"), ("30 дней", "30"), ("90 дней", "90")))
        elif action == "tariffs":
            self.page(user, f"30 дней · {self.bot.price()} ⭐\nПробный доступ · {self.bot.trial_days()} дн.",
                      [[button("Изменить цену", "a:price"), button("Пробный период", "a:trial")]])
        elif action in ("price", "trial"):
            desc = "Отправь новую цену в Stars." if action == "price" else "Отправь число дней пробного доступа. 0 отключает пробный доступ."
            self.prompt(user, action, desc, "a:tariffs")
        elif action == "sales":
            node, _, enabled = key.partition(":")
            self.bot.admin_action(user, "sales", {"node": node, "enabled": enabled == "1"}, self.bot.store)
        elif action == "payments":
            self.payments(user, self.bot.store)
        elif action == "findpayment":
            self.prompt(user, "refund", "Отправь номер платежа из списка.", "a:payments")
        elif action == "refund":
            self.bot.admin_action(user, action, {"payment": key}, self.bot.store)
        elif action == "settings":
            self.settings(user)
        elif action == "setting" and key in SETTINGS:
            self.prompt(user, "setting", SETTINGS[key][1], "a:settings", {"name": key})
        elif action == "clear" and key in {"welcome", "openai_key"}:
            self.save_setting(user, key, "")
            self.settings(user)
        elif action == "unified_off":
            self.prompt(user, "unified_off", "Отключение скроет единую ссылку. Дополнительные профили клиентов будут отозваны.",
                        "a:settings", choices=(("Подтвердить отключение", "confirm"),))
        elif action == "ai":
            if self.bot.setting("openai_key") and self.bot.setting("openai_model"):
                self.prompt(user, "ai", "Опиши задачу. Например, «проверь VPN-ноды» или «измени тариф на 200 Stars».", "admin")
            else:
                self.page(user, "Для ИИ-помощника укажи ключ и модель OpenAI в настройках.",
                          [[button("Открыть настройки", "a:settings")]])
        elif action == "audit":
            self.bot.audit(user)
        else:
            self.home(user)
