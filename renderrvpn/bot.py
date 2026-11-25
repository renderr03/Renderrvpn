import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import BoundedSemaphore

from .assistant import interpret
from .config import integer
from .menu import AdminMenu, USER_ID_MAX, back, button
from .services import Panel, ServiceError
from .store import Store


LOG = logging.getLogger(__name__)


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
        self.menu = AdminMenu(self)

    def send(self, chat, text, rows=None):
        payload = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
        if rows:
            payload["reply_markup"] = {"inline_keyboard": rows}
        return self.telegram.call("sendMessage", **payload)

    def setting(self, key, db=None):
        return (db or self.store).setting(key, getattr(self.config, key))

    def nodes(self, db=None):
        return (db or self.store).nodes(self.config.nodes)

    def price(self):
        return int(self.store.setting("price", self.config.price))

    def trial_days(self):
        return int(self.store.setting("trial", self.config.trial_days))

    def support(self, chat):
        name = self.setting("support")
        rows = [[{"text": "Написать в поддержку", "url": f"https://t.me/{name}"}]] if name else []
        text = "По вопросам подключения, оплаты и возвратов напиши владельцу сервиса." if name else "Владелец ещё настраивает контакт поддержки."
        self.send(chat, text, rows + back("home"))

    def terms(self, user, key=None):
        url = self.setting("terms_url")
        if not url or not self.setting("support"):
            raise ValueError("Оплата станет доступна после настройки условий и поддержки.")
        rows = [[{"text": "Открыть условия", "url": url}]]
        if key:
            rows.append([button("Принимаю условия · получить счёт", f"invoice:{key}")])
        self.send(user, "Условия покупки VPN" if key is None else "Прочитай условия и подтверди их, чтобы получить счёт.",
                  rows + back("shop"))

    def start(self, user):
        self.menu.leave(user)
        rows = [
            [button("Подключить VPN", "shop"), button("Моя подписка", "subscription")],
            [button("Как подключиться", "help"), button("Поддержка", "support")],
            [button("Мой Telegram ID", "myid")],
        ]
        if user in self.config.admins:
            rows.append([button("Админка", "admin")])
        welcome = self.setting("welcome")
        self.send(user, self.setting("brand") + ("\n\n" + welcome if welcome else ""), rows)

    def subscription(self, user):
        access = self.store.access(user)
        if not access:
            self.send(user, access_status(None), [[button("Подключить VPN", "shop")]] + back("home"))
            return
        node = self.nodes().get(access["node"])
        title = f"\nЛокация: {node.label}." if node else "\nЛокация требует настройки владельцем."
        text = access_status(access) + title
        if (
            node and access["desired_enabled"] and access["expires_at"] > time.time()
            and access["applied_revision"] > 0
        ):
            text += f"\n\nСсылка для импорта в VPN-приложение:\n{Panel(node).link(access)}"
        self.send(user, text, [[button("Продлить", "shop"), button("Как подключиться", "help")]] + back("home"))

    def shop(self, user):
        access = self.store.access(user)
        assigned = access["node"] if access else None
        nodes = [
            node for key, node in self.nodes().items()
            if key == assigned or (not assigned and self.store.sales_open(key))
        ]
        if not nodes:
            self.send(user, "Сейчас нет доступной локации.", [[button("Поддержка", "support")]] + back("home"))
            return
        trial = self.trial_days()
        used = self.store.one("SELECT 1 FROM entitlements WHERE user_id = ?", user)
        rows = []
        payment_open = bool(self.setting("terms_url") and self.setting("support"))
        for node in nodes:
            if payment_open:
                rows.append([button(f"{node.label} · 30 дней · {self.price()} ⭐", f"buy:{node.key}")])
            if trial > 0 and not used:
                rows.append([button(f"{node.label} · пробный доступ ({trial} дн.)", f"trial:{node.key}")])
        self.send(user, "Выбери локацию. Она закрепится при создании счёта или пробного доступа."
                  if rows else "Владелец ещё настраивает продажи.", rows + back("home"))

    def purchase(self, user, key, trial=False):
        node = self.nodes().get(key)
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
        if not self.setting("terms_url") or not self.setting("support"):
            raise ValueError("Владелец ещё настраивает продажи.")
        price = self.price()
        token = self.store.create_invoice(user, key, price, now)
        self.telegram.call(
            "sendInvoice", chat_id=user, title=f"{self.setting('brand')} · 30 дней",
            description=f"VPN в локации {node.label} на 30 дней.",
            payload=token, currency="XTR", prices=[{"label": "30 дней VPN", "amount": price}],
            start_parameter="vpn",
        )

    def precheckout(self, query):
        user = query["from"]["id"]
        access = self.store.access(user)
        accepted = bool(access and access["node"] in self.nodes()) and self.store.accept_checkout(
            query["invoice_payload"], user, query["currency"],
            query["total_amount"], query["id"], int(time.time()),
        )
        kwargs = {} if accepted else {"error_message": "Счёт устарел или уже оплачен. Открой магазин и создай новый счёт."}
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
            self.send(user, "Оплата требует сверки.", [[button("Связаться с поддержкой", "support")]])

    def audit(self, user):
        rows = self.store.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 12").fetchall()
        self.menu.page(user, "\n".join(f"{date(r['created_at'])} · ID {r['admin_id']} · {r['action']}"
                                     for r in rows) or "Журнал пока пуст.")

    def admin_action(self, user, action, data, db):
        if user not in self.config.admins:
            return
        if action == "help":
            self.menu.page(user, "Опиши одну задачу. Например, «проверь ноды» или «выдай клиенту 123456 доступ на 30 дней».",
                           [[button("Новая задача", "a:ai")]])
            return
        if action == "payments":
            self.menu.payments(user, db)
            return
        nodes = self.nodes(db)
        if action == "nodes":
            if data.get("node"):
                node = nodes.get(data["node"])
                if not node:
                    raise ValueError("Такой ноды нет в настройках.")
                selected = [node]
            else:
                selected = list(nodes.values())
            lines = []
            for node in selected:
                try:
                    _, health = Panel(node).health()
                except ServiceError as exc:
                    health = str(exc)
                sales = "открыты" if db.sales_open(node.key) else "закрыты"
                lines.append(f"{node.label} ({node.key}): {health}, новые продажи {sales}.")
            self.menu.page(user, "\n".join(lines) or "Сначала подключи панель 3x-ui.", parent="a:nodes")
            return
        if action in ("find", "grant", "retry", "revoke"):
            target = integer(data.get("user"), 1, USER_ID_MAX)
            access = db.access(target)
            if action == "find":
                node = nodes.get(access["node"]) if access else None
                text = f"Подписка клиента {target}\n{access_status(access)}" + (f"\nЛокация: {node.label}." if node else "")
                self.menu.client(user, target, text, bool(access and access["revision"]))
                return
            args = {"user": target}
            if action == "grant":
                days = integer(data.get("days"), 1, 366)
                key = data.get("node") or (access["node"] if access else None)
                if key is None and len(nodes) == 1:
                    key = next(iter(nodes))
                node = nodes.get(key)
                if not node or (access and access["node"] != key):
                    raise ValueError("Укажи локацию клиента. Локации видны в разделе «Панели и ноды».")
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
            node = nodes.get(data.get("node"))
            enabled = data.get("enabled")
            if isinstance(enabled, str):
                enabled = {"on": True, "off": False}.get(enabled)
            if not node or type(enabled) is not bool:
                raise ValueError("Выбери локацию и действие в админке.")
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

    def admin_task(self, user, callback, on_error=None):
        if not self.task_slots.acquire(blocking=False):
            self.menu.page(user, "Выполняются другие запросы администратора. Попробуй чуть позже.")
            return False

        def run():
            db = None
            try:
                db = Store(self.config.database)
                callback(db)
            except (ServiceError, ValueError) as exc:
                try:
                    if on_error:
                        on_error(str(exc))
                    else:
                        self.menu.page(user, str(exc))
                except ServiceError:
                    LOG.warning("Ответ администратора не доставлен")
            except Exception:
                LOG.exception("Фоновая операция администратора завершилась ошибкой")
            finally:
                if db is not None:
                    db.db.close()
                self.task_slots.release()

        self.tasks.submit(run)
        return True

    def background_admin(self, user, action, data):
        def run(db):
            if action == "assistant":
                cfg = replace(self.config, nodes=self.nodes(db), openai_key=self.setting("openai_key", db),
                              openai_model=self.setting("openai_model", db))
                result = interpret(cfg, data["message"])
                self.admin_action(user, result["action"], result, db)
            else:
                self.admin_action(user, action, data, db)

        self.admin_task(user, run)

    def client_help(self, user):
        self.send(
            user,
            "1. Открой «Моя подписка» и скопируй ссылку.\n"
            "2. Установи VPN-приложение с поддержкой VLESS и подписок 3x-ui для своей системы.\n"
            "3. В приложении выбери импорт подписки из URL, вставь ссылку и подключись.",
            [[button("Моя подписка", "subscription"), button("Поддержка", "support")]] + back("home"),
        )

    def callback(self, user, action):
        head, _, key = action.partition(":")
        if head == "home":
            self.start(user)
        elif head == "shop":
            self.menu.leave(user)
            self.shop(user)
        elif head == "subscription":
            self.menu.leave(user)
            self.subscription(user)
        elif head == "help":
            self.client_help(user)
        elif head == "support":
            self.support(user)
        elif head == "myid":
            rows = [[{"text": "Скопировать ID", "copy_text": {"text": str(user)}}]]
            self.send(user, f"Твой Telegram ID\n{user}", rows + back("home"))
        elif head == "buy":
            self.terms(user, key)
        elif head in ("invoice", "trial"):
            self.purchase(user, key, head == "trial")
        elif user in self.config.admins:
            if head == "confirm":
                self.menu.page(user, self.store.confirm(user, key))
            elif head == "cancel":
                text = "Действие отменено." if self.store.cancel(user, key) else "Это подтверждение уже закрыто."
                self.menu.page(user, text)
            elif head == "admin":
                self.menu.home(user)
            elif head == "a":
                self.menu.handle_button(user, key)
            elif head == "w":
                nonce, _, choice = key.partition(":")
                self.menu.choose(user, nonce, choice)

    def message(self, user, message):
        if payment := message.get("successful_payment"):
            self.successful_payment(user, payment, message["date"])
            return
        raw = message.get("text", "").strip()
        if not raw:
            return
        command = raw.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        if command == "/start":
            self.start(user)
        elif command == "/paysupport":
            self.support(user)
        elif user in self.config.admins and self.menu.current(user):
            self.menu.message(user, raw, message["message_id"])
        else:
            self.start(user)

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
            draft = self.menu.current(user) if user in self.config.admins else None
            self.send(user, str(exc), back(draft.parent if draft else ("admin" if user in self.config.admins else "home")))
