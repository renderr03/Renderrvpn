import tempfile
import time
import unittest
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
        if service != "3x-ui" or token != "secret-panel-token":
            raise AssertionError("wrong service or token")
        if self.offline:
            raise ServiceError("3x-ui", "нет соединения")
        route = url.split("/panel/api/")[1]
        if route.startswith("clients/get/tgId/"):
            target = int(route.rsplit("/", 1)[1])
            records = [v for v in self.records.values() if v["client"]["tgId"] == target]
            return {"success": True, "obj": records}
        if route == "inbounds/get/7":
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
        self.bot.handle({"message": {"chat": chat, "from": {"id": user}, "text": "/subscription"}})
        text = self.telegram.calls[-1][1]["text"]
        self.assertIn(Panel(self.node).link(self.store.access(user)), text)
        self.assertNotIn(self.node.token, text)
        self.bot.handle({"message": {"chat": chat, "refunded_payment": payment}})
        self.assertEqual(self.store.payment("charge-tg")["refund_state"], "refunded")
        self.apply()
        self.assertFalse(next(iter(self.panel.records.values()))["client"]["enable"])

    def test_admin_only_in_private_chat(self):
        self.bot.handle({"message": {"chat": {"id": 5, "type": "group"}, "from": {"id": 5}, "text": "/price 17"}})
        self.bot.handle({"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/price 17"}})
        self.assertFalse(self.telegram.calls)
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
