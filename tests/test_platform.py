import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from renderrvpn.__main__ import synchronize_access, synchronize_refund
from renderrvpn.bot import Bot
from renderrvpn.config import Config, Node
from renderrvpn.services import Panel, ServiceError
from renderrvpn.store import DAY, Store


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.already_refunded = False

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "refundStarPayment" and self.already_refunded:
            raise ServiceError("Telegram", 400, "Bad Request: CHARGE_ALREADY_REFUNDED")
        return True


class FakePanel:
    def __init__(self):
        self.records = {}
        self.writes = []
        self.offline = False
        self.lose_response = False

    def request(self, service, url, *, payload, token):
        if service != "3x-ui" or token not in {"secret-panel-token", "rotated-token"}:
            raise AssertionError("wrong service or token")
        if self.offline:
            raise ServiceError("3x-ui", "нет соединения")
        route = url.split("/panel/api/")[1]
        if route.startswith("clients/get/tgId/"):
            target = int(route.rsplit("/", 1)[1])
            records = [v for v in self.records.values() if v["client"]["tgId"] == target]
            return {"success": True, "obj": records}
        if route == "inbounds/options":
            return {"success": True, "obj": [
                {"id": 7, "remark": "Германия", "protocol": "vless", "tlsFlowCapable": True},
                {"id": 8, "remark": "Нидерланды", "protocol": "vless", "tlsFlowCapable": False},
            ]}
        if route == "setting/all":
            return {"success": True, "obj": {"subURI": "https://sub.test/sub/"}}
        if route in {"inbounds/get/7", "inbounds/get/8"}:
            return {"success": True, "obj": {"enable": True, "protocol": "vless"}}
        if route == "server/status":
            return {"success": True, "obj": {"xray": {"state": "running"}}}
        if route == "clients/add":
            email = payload["client"]["email"]
            if email in self.records:
                raise AssertionError("duplicate create")
            fields = dict(payload["client"])
            fields.update(uuid=fields.pop("id"), id=91)
            self.records[email] = {"client": fields, "inboundIds": payload["inboundIds"]}
        elif route.startswith("clients/update/"):
            email = route.rsplit("/", 1)[1]
            fields = dict(payload)
            if fields.pop("id") != self.records[email]["client"]["uuid"]:
                raise AssertionError("VLESS UUID changed")
            self.records[email]["client"].update(fields)
        else:
            raise AssertionError("unexpected panel route")
        self.writes.append(route)
        if self.lose_response:
            self.lose_response = False
            raise ServiceError("3x-ui", "нет соединения")
        return {"success": True}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "bot.sqlite3"
        self.node = Node("de", "Германия", "https://panel.test/hidden", "https://sub.test/sub", 7, "secret-panel-token")
        self.config = Config("fake", frozenset({5}), "RenderrVPN", "", "support_test", "https://sub.test/terms",
                             True, 150, 3, {"de": self.node}, path, "", "")
        self.store = Store(path)
        self.addCleanup(self.store.db.close)
        self.telegram = FakeTelegram()
        self.panel = FakePanel()
        local = patch("renderrvpn.services.request_json", side_effect=self.panel.request)
        local.start()
        self.addCleanup(local.stop)
        self.bot = Bot(self.config, self.store, self.telegram)
        self.addCleanup(self.bot.tasks.shutdown)
        self.now = int(time.time())

    def checkout(self, user, index, now=None):
        now = self.now if now is None else now
        invoice = self.store.create_invoice(user, "de", 150, now)
        checkout_id = f"checkout-{index}"
        self.assertTrue(self.store.accept_checkout(invoice, user, "XTR", 150, checkout_id, now))
        result = self.store.record_payment(invoice, user, "XTR", 150, f"charge-{index}", now)
        self.assertEqual(result, "paid")
        return invoice

    def apply(self, now=None):
        now = self.now if now is None else now
        row = self.store.next_access(now)
        self.assertIsNotNone(row)
        synchronize_access(self.store, self.bot, row, now)

    def click(self, user, action, *, chat_type="private"):
        chat = {"id": user, "type": chat_type}
        self.bot.handle({"callback_query": {"id": "cb", "from": {"id": user},
                                           "message": {"chat": chat}, "data": action}})

    def speak(self, user, text):
        self.bot.handle({"message": {"chat": {"id": user, "type": "private"}, "from": {"id": user},
                                     "message_id": 1, "text": text}})

    def button(self, text):
        rows = self.telegram.calls[-1][1]["reply_markup"]["inline_keyboard"]
        return next(btn["callback_data"] for row in rows for btn in row if text in btn["text"])

    def click_label(self, text):
        self.click(5, self.button(text))

    def inline_admin_task(self, user, callback, on_error=None):
        try:
            callback(self.store)
        except (ValueError, ServiceError) as exc:
            if on_error:
                on_error(str(exc))
            else:
                raise
        return True

    def test_install_config_accepts_only_bot_token_and_admin_id(self):
        env = {"TELEGRAM_BOT_TOKEN": "123456:exampleTokenPlaceholder00", "ADMIN_IDS": "5",
               "NODES_FILE": str(Path(self.tmp.name) / "absent.json")}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.admins, {5})
        self.assertEqual(config.nodes, {})
        self.assertEqual(config.support, "")
        self.assertEqual(config.terms_url, "")

    def test_connect_panels_use_sqlite_and_can_grant_from_buttons(self):
        self.bot.config = replace(self.config, nodes={}, support="", terms_url="")
        with patch.object(self.bot, "admin_task", side_effect=self.inline_admin_task):
            self.speak(5, "/start")
            self.click_label("Админка")
            self.click_label("Добавить основную панель")
            self.speak(5, "https://panel.test/hidden/panel/inbounds")
            self.speak(5, "secret-panel-token")
            self.assertEqual(self.store.nodes({}), {})
            self.click_label("Германия")
            self.click_label("Подключить ноду")
            saved, = self.store.nodes({}).values()
            self.assertEqual(saved.api_url, "https://panel.test/hidden")
            self.assertEqual(saved.subscription_url, "https://sub.test/sub")
            self.assertEqual(saved.inbound_id, 7)
            self.assertEqual(saved.flow, "xtls-rprx-vision")
            self.assertIn("deleteMessage", [event for event, _ in self.telegram.calls])
            self.assertFalse(any("secret-panel-token" in str(value) for _, value in self.telegram.calls))
            self.click(5, "a:addnode")
            self.click_label("Из панели")
            self.click_label("Нидерланды")
            self.click_label("Подключить ноду")
            self.assertEqual({n.inbound_id for n in self.store.nodes({}).values()}, {7, 8})
            self.click(5, f"a:rotate:{saved.key}")
            self.speak(5, "rotated-token")
            self.click_label("Сохранить")
            self.assertEqual({n.token for n in self.store.nodes({}).values()}, {"rotated-token"})
            self.click(5, "a:clients")
            self.speak(5, "123")
            self.click_label("Выдать или продлить")
            self.click_label("30 дней")
            self.click_label("Германия")
            self.click_label("Подтвердить")
            self.apply()
            self.click(123, "subscription")
            self.assertIn("https://sub.test/sub/", self.telegram.calls[-1][1]["text"])

    def test_button_only_settings_back_and_cancel(self):
        self.click(5, "a:price")
        self.click_label("Отмена")
        self.speak(5, "17")
        self.assertIsNone(self.store.one("SELECT * FROM approvals"))
        self.click(5, "a:tariffs")
        self.click_label("Изменить цену")
        self.speak(5, "17")
        confirm = self.button("Подтвердить")
        self.click_label("Отмена")
        self.click(5, confirm)
        self.assertEqual(self.bot.price(), 150)
        self.click(5, "a:tariffs")
        self.click_label("Изменить цену")
        self.speak(5, "175")
        self.click_label("Подтвердить")
        self.assertEqual(self.bot.price(), 175)
        self.click(5, "a:settings")
        self.click_label("Контакт поддержки")
        self.speak(5, "@new_support")
        self.assertEqual(self.bot.setting("support"), "new_support")
        self.click(123, "support")
        self.assertEqual(self.telegram.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"], "https://t.me/new_support")

    def test_cancelled_node_draft_cannot_be_saved(self):
        self.bot.config = replace(self.config, nodes={})
        with patch.object(self.bot, "admin_task", side_effect=self.inline_admin_task):
            self.click(5, "a:addnode")
            self.speak(5, "http://127.0.0.1:2053/hidden")
            self.speak(5, "secret-panel-token")
            self.click_label("Германия")
            save = self.button("Подключить ноду")
            self.click_label("Отмена")
            self.click(5, save)
        self.assertEqual(self.store.nodes({}), {})

    def test_payment_is_idempotent_and_renewal_keeps_link(self):
        invoice = self.checkout(123, 1)
        self.assertEqual(self.store.record_payment(invoice, 123, "XTR", 150, "charge-1", self.now), "duplicate")
        self.assertFalse(self.store.accept_checkout(invoice, 123, "XTR", 150, "new", self.now))
        self.apply()
        first = self.store.access(123)
        self.assertEqual(first["expires_at"], self.now + 30 * DAY)
        self.checkout(123, 2, self.now + DAY)
        self.apply(self.now + DAY)
        renewed = self.store.access(123)
        self.assertEqual(renewed["expires_at"], self.now + 60 * DAY)
        self.assertEqual(Panel(self.node).link(first), Panel(self.node).link(renewed))
        self.assertEqual(first["client_uuid"], renewed["client_uuid"])
        self.assertEqual(len(self.panel.records), 1)
        self.assertEqual(self.panel.records[first["panel_email"]]["client"]["expiryTime"], renewed["expires_at"] * 1000)

    def test_checkout_rejects_wrong_recipient_currency_price_and_expiry(self):
        invoice = self.store.create_invoice(123, "de", 150, self.now)
        for user, currency, amount, when in [
            (124, "XTR", 150, self.now), (123, "EUR", 150, self.now),
            (123, "XTR", 151, self.now), (123, "XTR", 150, self.now + 1801),
        ]:
            self.assertFalse(self.store.accept_checkout(invoice, user, currency, amount, "id", when))
        self.assertEqual(self.store.record_payment(invoice, 123, "XTR", 150, "unapproved", self.now), "mismatch")
        self.assertIsNone(self.store.next_access(self.now))

    def test_panel_recovery_reuses_provisioned_client_after_lost_response(self):
        self.checkout(123, 1)
        self.panel.offline = True
        self.apply()
        self.assertEqual(self.store.access(123)["applied_revision"], 0)
        self.panel.offline = False
        self.panel.lose_response = True
        self.apply(self.now + 30)
        self.assertEqual(len(self.panel.records), 1)
        self.assertEqual(self.store.access(123)["applied_revision"], 0)
        self.apply(self.now + 60)
        self.assertEqual(self.store.access(123)["applied_revision"], 1)
        self.assertEqual(self.panel.writes, ["clients/add"])

    def test_refund_preserves_other_entitlement_and_recovers_after_lost_response(self):
        self.checkout(123, 1)
        self.checkout(123, 2, self.now + DAY)
        token = self.store.approval(5, "refund", {"charge": "charge-2"})
        self.store.confirm(5, token)
        self.assertIsNotNone(self.store.next_refund(self.now))
        self.telegram.already_refunded = True
        synchronize_refund(self.store, self.telegram, self.store.next_refund(self.now), self.now)
        self.assertEqual(self.store.payment("charge-2")["refund_state"], "refunded")
        self.assertEqual(self.store.access(123)["expires_at"], self.now + 30 * DAY)
        self.assertFalse(self.store.finish_refund("charge-2", 123))
        self.assertFalse(self.store.finish_refund("charge-1", 124))
        self.assertTrue(self.store.finish_refund("charge-1", 123))
        self.assertEqual(self.store.access(123)["desired_enabled"], 0)

    def test_trial_only_once_and_approval_binds_to_owner(self):
        self.store.start_trial(123, "de", 3, self.now)
        with self.assertRaises(ValueError):
            self.store.start_trial(123, "de", 3, self.now)
        token = self.store.approval(5, "revoke", {"user": 123})
        self.store.confirm(6, token)
        self.assertTrue(self.store.access(123)["desired_enabled"])
        self.store.confirm(5, token)
        self.assertFalse(self.store.access(123)["desired_enabled"])
        self.assertIn("использовано", self.store.confirm(5, token))
        cancelled = self.store.approval(5, "price", {"amount": 1})
        self.assertTrue(self.store.cancel(5, cancelled))
        self.assertIn("использовано", self.store.confirm(5, cancelled))
        self.assertIsNone(self.store.setting("price"))

    def test_real_telegram_message_shapes_and_terms_checkpoint(self):
        user = 123
        chat = {"id": user, "type": "private"}
        self.bot.handle({"callback_query": {"id": "c-1", "from": {"id": user},
                                           "message": {"chat": chat}, "data": "buy:de"}})
        self.assertFalse(any(name == "sendInvoice" for name, _ in self.telegram.calls))
        agreement = self.telegram.calls[-1][1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        self.bot.handle({"callback_query": {"id": "c-2", "from": {"id": user},
                                           "message": {"chat": chat}, "data": agreement}})
        name, invoice = self.telegram.calls[-1]
        self.assertEqual(name, "sendInvoice")
        payment = {"invoice_payload": invoice["payload"], "total_amount": 150, "currency": "XTR",
                   "telegram_payment_charge_id": "charge-tg"}
        self.bot.handle({"pre_checkout_query": {**payment, "id": "pc", "from": {"id": user}}})
        self.assertTrue(self.telegram.calls[-1][1]["ok"])
        self.bot.handle({"message": {"chat": chat, "from": {"id": user}, "date": self.now,
                                     "successful_payment": payment}})
        self.apply()
        self.click(user, "subscription")
        text = self.telegram.calls[-1][1]["text"]
        self.assertIn(Panel(self.node).link(self.store.access(user)), text)
        self.assertNotIn(self.node.token, text)
        self.bot.handle({"message": {"chat": chat, "refunded_payment": payment}})
        self.assertEqual(self.store.payment("charge-tg")["refund_state"], "refunded")
        self.apply()
        self.assertFalse(next(iter(self.panel.records.values()))["client"]["enable"])

    def test_admin_only_in_private_chat(self):
        self.click(5, "a:price", chat_type="group")
        self.click(7, "a:price")
        self.assertTrue(all(method == "answerCallbackQuery" for method, _ in self.telegram.calls))
        self.assertIsNone(self.store.one("SELECT * FROM approvals"))

    def test_wrong_upstream_client_identity_does_not_mutate_panel(self):
        self.checkout(123, 1)
        self.apply()
        access = self.store.access(123)
        self.panel.records[access["panel_email"]]["client"]["uuid"] = "different"
        self.checkout(123, 2)
        self.apply()
        self.assertEqual(self.panel.writes, ["clients/add"])
        self.assertEqual(self.store.access(123)["applied_revision"], 1)


if __name__ == "__main__":
    unittest.main()
