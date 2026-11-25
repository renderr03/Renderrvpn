import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import BoundedSemaphore

from .assistant import interpret
from .config import integer
from .services import Panel, ServiceError
from .store import Store


LOG = logging.getLogger(__name__)
USER_ID_MAX = 2**52 - 1
ADMIN_HELP = (
    "/client ID — найти подписку\n"
    "/grant ID ДНИ [НОДА] — выдать или продлить доступ\n"
    "/revoke ID — отключить доступ\n"
    "/retry ID — повторить синхронизацию с панелью\n"
    "/price STARS — изменить цену на 30 дней\n"
    "/trial ДНИ — настроить пробный период (0 отключает)\n"
    "/sales НОДА on|off — открыть или закрыть новые продажи\n"
    "/refund НОМЕР — вернуть платёж из списка\n"
    "/audit — журнал изменений\n"
)
COMMANDS = {
    "/client": ("find", ("user",)),
    "/grant": ("grant", ("user", "days", "node")),
    "/revoke": ("revoke", ("user",)),
    "/retry": ("retry", ("user",)),
    "/price": ("price", ("amount",)),
    "/trial": ("trial", ("days",)),
    "/sales": ("sales", ("node", "enabled")),
    "/refund": ("refund", ("payment",)),
}


def button(title, action):
    return {"text": title, "callback_data": action}


def date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def access_status(access):
    if not access or access["revision"] == 0:
        return "Подписки пока нет."
    suffix = ""
    if access["revision"] > access["applied_revision"]:
        suffix = "\nОбновление профиля на стороне 3x-ui в очереди."
        if access["last_error"]:
            suffix += f"\nПоследний ответ: {access['last_error']}. Бот попробует ещё раз."
    if not access["desired_enabled"]:
        return "Доступ отключён." + suffix
    if access["expires_at"] <= time.time():
        return f"Подписка закончилась {date(access['expires_at'])}." + suffix
    return f"Доступ до {date(access['expires_at'])}." + suffix


class Bot:
    def __init__(self, config, store, telegram):
        self.config = config
        self.store = store
        self.telegram = telegram
        self.tasks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin")
        self.task_slots = BoundedSemaphore(2)

    def send(self, chat, text, rows=None):
        payload = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
        return self.telegram.call("sendMessage", **payload)

    def price(self):
        return int(self.store.setting("price", self.config.price))

    def trial_days(self):
        return int(self.store.setting("trial", self.config.trial_days))

    def support(self, chat):
        self.send(
            chat, "По вопросам подключения, оплаты и возвратов напиши владельцу сервиса через кнопку ниже.",
            [[{"text": "Написать в поддержку", "url": f"https://t.me/{self.config.support}"}]],
        )

    def terms(self, user, key=None):
        rows = [[{"text": "Открыть условия", "url": self.config.terms_url}]]
        if key:
            rows.append([button("Принимаю условия · получить счёт", f"invoice:{key}")])
        self.send(user, "Условия покупки VPN" if key is None else "Прочитай условия и подтверди их, чтобы получить счёт.", rows)

    def start(self, user):
        rows = [
            [button("Подключить VPN", "shop"), button("Моя подписка", "subscription")],
            [button("Как подключиться", "help"), button("Поддержка", "support")],
        ]
        if user in self.config.admins:
            rows.append([button("Админка", "admin")])
        self.send(user, self.config.brand + ("\n\n" + self.config.welcome if self.config.welcome else ""), rows)

    def subscription(self, user):
        access = self.store.access(user)
        if not access:
            self.send(user, access_status(None), [[button("Подключить VPN", "shop")]])
            return
        node = self.config.nodes.get(access["node"])
        title = f"\nЛокация: {node.label}." if node else "\nЛокация требует настройки владельцем."
        text = access_status(access) + title
        if (
            node and access["desired_enabled"] and access["expires_at"] > time.time()
            and access["applied_revision"] > 0
        ):
            text += f"\n\nСсылка для импорта в VPN-приложение:\n{Panel(node).link(access)}"
        self.send(user, text, [[button("Продлить", "shop"), button("Как подключиться", "help")]])

    def shop(self, user):
        access = self.store.access(user)
        assigned = access["node"] if access else None
        nodes = [
            node for key, node in self.config.nodes.items()
            if key == assigned or (not assigned and self.store.sales_open(key))
        ]
        if not nodes:
            self.send(user, "Сейчас нет доступной локации. Напиши в поддержку.")
            return
        trial = self.trial_days()
        used = self.store.one("SELECT 1 FROM entitlements WHERE user_id = ?", user)
        rows = []
        for node in nodes:
            rows.append([button(f"{node.label} · 30 дней · {self.price()} ⭐", f"buy:{node.key}")])
            if trial > 0 and not used:
                rows.append([button(f"{node.label} · пробный доступ ({trial} дн.)", f"trial:{node.key}")])
        self.send(user, "Выбери локацию. Она закрепится при создании счёта или пробного доступа.", rows)

    def purchase(self, user, key, trial=False):
        node = self.config.nodes.get(key)
        access = self.store.access(user)
        if not node or (not self.store.sales_open(key) and not (access and access["node"] == key)):
            raise ValueError("Эта локация недоступна для новых подключений.")
        now = int(time.time())
        if trial:
            days = self.trial_days()
            if days == 0:
                raise ValueError("Пробный доступ сейчас отключён.")
            self.store.start_trial(user, key, days, now)
            self.send(user, "Пробный доступ готовится.", [[button("Моя подписка", "subscription")]])
            return
        price = self.price()
        token = self.store.create_invoice(user, key, price, now)
        self.telegram.call(
            "sendInvoice", chat_id=user, title=f"{self.config.brand} · 30 дней",
            description=f"VPN в локации {node.label} на 30 дней.",
            payload=token, currency="XTR", prices=[{"label": "30 дней VPN", "amount": price}],
            start_parameter="vpn",
        )

    def precheckout(self, query):
        user = query["from"]["id"]
        access = self.store.access(user)
        accepted = bool(access and access["node"] in self.config.nodes) and self.store.accept_checkout(
            query["invoice_payload"], user, query["currency"],
            query["total_amount"], query["id"], int(time.time()),
        )
        kwargs = {} if accepted else {"error_message": "Счёт устарел или уже оплачен. Создай новый через /start."}
        self.telegram.call("answerPreCheckoutQuery", pre_checkout_query_id=query["id"], ok=accepted, **kwargs)

    def successful_payment(self, user, payment, timestamp):
        state = self.store.record_payment(
            payment["invoice_payload"], user, payment["currency"], payment["total_amount"],
            payment["telegram_payment_charge_id"], timestamp,
        )
        if state == "paid":
            self.send(user, "Оплата получена. Ссылка появится в «Моя подписка» после настройки 3x-ui.",
                      [[button("Моя подписка", "subscription")]])
        elif state == "mismatch":
            LOG.error("Получен платёж, не совпавший с выставленным счётом")
            for admin in self.config.admins:
                try:
                    self.send(admin, f"Платёж клиента {user} требует ручной сверки в Telegram.")
                except ServiceError:
                    LOG.warning("Не удалось уведомить администратора")
            self.send(user, "Оплата требует сверки. Используй /paysupport.")

    def admin_menu(self, user):
        rows = [
            [button("Клиенты", "admin:find"), button("Тарифы", "admin:price")],
            [button("Серверы", "admin:nodes"), button("Платежи", "admin:payments")],
        ]
        if self.config.openai_key:
            rows.append([button("ИИ-помощник", "admin:assistant")])
        rows.append([button("Команды и журнал", "admin:help")])
        self.send(user, "Управление VPN-магазином", rows)

    def payments(self, db):
        rows = db.db.execute(
            "SELECT rowid AS number, * FROM payments ORDER BY paid_at DESC, number DESC LIMIT 12"
        ).fetchall()
        statuses = {"paid": "оплачен", "refund_pending": "возврат в очереди", "refunded": "возвращён"}
        return "\n".join(
            f"#{r['number']} · ID {r['user_id']} · {r['amount']} ⭐ · {statuses[r['refund_state']]}"
            + (f" · {r['last_error']}" if r["last_error"] else "")
            for r in rows
        ) or "Платежей пока нет."

    def admin_action(self, user, action, data, db):
        if user not in self.config.admins:
            return
        if action == "help":
            self.send(user, ADMIN_HELP)
            return
        if action == "payments":
            self.send(user, self.payments(db) + "\n\nВозврат: /refund НОМЕР.")
            return
        if action == "nodes":
            if data.get("node"):
                node = self.config.nodes.get(data["node"])
                if not node:
                    raise ValueError("Такой ноды нет в настройках.")
                nodes = [node]
            else:
                nodes = list(self.config.nodes.values())
            lines = []
            for node in nodes:
                try:
                    _, health = Panel(node).health()
                except ServiceError as exc:
                    health = str(exc)
                sales = "открыты" if db.sales_open(node.key) else "закрыты"
                lines.append(f"{node.label} ({node.key}): {health}, новые продажи {sales}.")
            self.send(user, "\n".join(lines))
            return
        if action in ("find", "grant", "retry", "revoke"):
            target = integer(data.get("user"), 1, USER_ID_MAX)
            access = db.access(target)
            if action == "find":
                node = self.config.nodes.get(access["node"]) if access else None
                self.send(user, f"Подписка клиента {target}\n{access_status(access)}"
                          + (f"\nЛокация: {node.label} ({node.key})." if node else ""))
                return
            args = {"user": target}
            if action == "grant":
                days = integer(data.get("days"), 1, 366)
                key = data.get("node") or (access["node"] if access else None)
                if key is None and len(self.config.nodes) == 1:
                    key = next(iter(self.config.nodes))
                node = self.config.nodes.get(key)
                if not node or (access and access["node"] != key):
                    raise ValueError("Укажи ключ ноды клиента. Ключи видны в разделе «Серверы».")
                args.update(days=days, node=key)
                summary = f"Добавить {days} дн. доступа клиенту {target} на ноде {node.label}?"
            elif not access or access["revision"] == 0:
                raise ValueError("Подписка не найдена.")
            elif action == "retry":
                summary = f"Повторить синхронизацию подписки клиента {target} с панелью?"
            else:
                summary = f"Отключить доступ клиента {target}? Оплата при этом сохраняется."
        elif action == "price":
            amount = integer(data.get("amount"), 1, 10_000)
            args = {"amount": amount}
            summary = f"Установить цену {amount} ⭐ за 30 дней для новых счетов?"
        elif action == "trial":
            days = integer(data.get("days"), 0, 30)
            args = {"days": days}
            summary = f"Установить пробный период {days} дн.?" if days else "Отключить пробный период?"
        elif action == "sales":
            node = self.config.nodes.get(data.get("node"))
            enabled = data.get("enabled")
            if isinstance(enabled, str):
                enabled = {"on": True, "off": False}.get(enabled)
            if not node or type(enabled) is not bool:
                raise ValueError("Укажи ключ ноды и on или off.")
            args = {"node": node.key, "enabled": enabled}
            verb = "Открыть" if enabled else "Закрыть"
            summary = f"{verb} новые продажи на ноде {node.label}? Продление остаётся доступным."
        elif action == "refund":
            payment = integer(data.get("payment"), 1, 2**52 - 1)
            row = db.one("SELECT * FROM payments WHERE rowid = ?", payment)
            if not row or row["refund_state"] == "refunded":
                raise ValueError("Платёж не найден или уже возвращён.")
            args = {"charge": row["charge"]}
            summary = f"Вернуть {row['amount']} ⭐ клиенту {row['user_id']} по платежу #{payment}?"
        else:
            raise ValueError("Неизвестная операция.")
        token = db.approval(user, action, args)
        self.send(user, summary, [[button("Подтвердить", f"confirm:{token}"), button("Отмена", f"cancel:{token}")]])

    def background_admin(self, user, action, data):
        if not self.task_slots.acquire(blocking=False):
            self.send(user, "Выполняются другие запросы администратора. Попробуй чуть позже.")
            return

        def run():
            db = None
            try:
                db = Store(self.config.database)
                if action == "assistant":
                    result = interpret(self.config, data["message"])
                    self.admin_action(user, result["action"], result, db)
                else:
                    self.admin_action(user, action, data, db)
            except (ServiceError, ValueError) as exc:
                try:
                    self.send(user, str(exc))
                except ServiceError:
                    LOG.warning("Ответ администратора не доставлен")
            except Exception:
                LOG.exception("Фоновая операция администратора завершилась ошибкой")
            finally:
                if db is not None:
                    db.db.close()
                self.task_slots.release()

        self.tasks.submit(run)

    def client_help(self, user):
        self.send(
            user,
            "1. Открой «Моя подписка» и скопируй ссылку.\n"
            "2. Установи VPN-приложение с поддержкой VLESS и подписок 3x-ui для своей системы.\n"
            "3. В приложении выбери импорт подписки из URL, вставь ссылку и подключись.",
            [[button("Моя подписка", "subscription"), button("Поддержка", "support")]],
        )

    def callback(self, user, action):
        head, _, key = action.partition(":")
        if head == "shop":
            self.shop(user)
        elif head == "subscription":
            self.subscription(user)
        elif head == "help":
            self.client_help(user)
        elif head == "support":
            self.support(user)
        elif head == "buy":
            self.terms(user, key)
        elif head in ("invoice", "trial"):
            self.purchase(user, key, head == "trial")
        elif user in self.config.admins:
            if head == "confirm":
                self.send(user, self.store.confirm(user, key))
            elif head == "cancel":
                if self.store.cancel(user, key):
                    self.send(user, "Действие отменено.")
                else:
                    self.send(user, "Это подтверждение уже закрыто.")
            elif head == "admin":
                if key == "nodes":
                    self.background_admin(user, "nodes", {})
                elif key == "payments":
                    self.admin_action(user, "payments", {}, self.store)
                elif key == "find":
                    self.send(user, "Поиск: /client ID\nВыдать доступ: /grant ID ДНИ [НОДА].")
                elif key == "price":
                    self.send(user, f"30 дней: {self.price()} ⭐. Пробный доступ: {self.trial_days()} дн.\n"
                              "Изменить цену: /price STARS\nПробный период: /trial ДНИ.")
                elif key == "assistant" and self.config.openai_key:
                    self.send(user, "Напиши /ai и задачу. Например: /ai проверь VPN-ноды.")
                elif key == "help":
                    self.send(user, ADMIN_HELP)
                else:
                    self.admin_menu(user)

    def message(self, user, message):
        if payment := message.get("successful_payment"):
            self.successful_payment(user, payment, message["date"])
            return
        raw = message.get("text", "").strip()
        command, _, params = raw.partition(" ")
        command = command.split("@", maxsplit=1)[0]
        if command == "/start":
            self.start(user)
        elif command == "/subscription":
            self.subscription(user)
        elif command == "/terms":
            self.terms(user)
        elif command in ("/paysupport", "/support"):
            self.support(user)
        elif user in self.config.admins:
            if command == "/admin":
                self.admin_menu(user)
            elif command == "/nodes":
                self.background_admin(user, "nodes", {})
            elif command == "/audit":
                rows = self.store.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 12").fetchall()
                self.send(user, "\n".join(f"{date(r['created_at'])} · ID {r['admin_id']} · {r['action']}"
                                          for r in rows) or "Журнал пока пуст.")
            elif command in COMMANDS:
                action, names = COMMANDS[command]
                values = params.split()
                min_args = len(names) - (action == "grant")
                if not min_args <= len(values) <= len(names):
                    self.send(user, ADMIN_HELP)
                else:
                    self.admin_action(user, action, dict(zip(names, values)), self.store)
            elif command == "/ai" and self.config.openai_key and 0 < len(params) <= 1500:
                self.background_admin(user, "assistant", {"message": params})
            elif command == "/ai":
                self.send(user, "Для помощника нужен настроенный API OpenAI и запрос до 1500 символов.")

    def handle(self, update):
        if query := update.get("pre_checkout_query"):
            self.precheckout(query)
            return
        if query := update.get("callback_query"):
            try:
                self.telegram.call("answerCallbackQuery", callback_query_id=query["id"])
            except ServiceError:
                LOG.info("Кнопка устарела или Telegram задержал подтверждение")
            user = query["from"]["id"]
            chat = query.get("message", {}).get("chat", {})
            if chat.get("type") != "private" or chat.get("id") != user:
                return
            action = lambda: self.callback(user, query.get("data", ""))
        elif message := update.get("message"):
            chat = message["chat"]
            user = message.get("from", {}).get("id")
            if chat["type"] != "private":
                return
            if refund := message.get("refunded_payment"):
                if refund["currency"] == "XTR":
                    self.store.finish_refund(refund["telegram_payment_charge_id"], chat["id"])
                return
            if chat["id"] != user:
                return
            action = lambda: self.message(user, message)
        else:
            return
        try:
            action()
        except ValueError as exc:
            self.send(user, str(exc))
