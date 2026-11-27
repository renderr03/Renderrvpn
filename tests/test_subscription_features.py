import base64
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from renderrvpn.bot import Bot
from renderrvpn.config import Config, Node
from renderrvpn.jobs import notify_health, remind, synchronize_placement
from renderrvpn.services import Panel, ServiceError, Telegram, qr_png
from renderrvpn.store import DAY, Store
from renderrvpn.subscriptions import SubscriptionHandler


class MemorySocket:
    def __init__(self, route):
        self.input = io.BytesIO(f"GET {route} HTTP/1.0\r\nHost: vpn.test\r\n\r\n".encode())
        self.output = io.BytesIO()

    def settimeout(self, timeout):
        pass

    def makefile(self, mode, size):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class SubscriptionFeaturesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bot.sqlite3"
        self.primary = Node("de", "Германия", "https://panel.test", "https://vpn.test/de", 7, "token-1")
        self.replica = Node("nl", "Нидерланды", "https://panel2.test", "https://vpn.test/nl", 8, "token-2")
        self.nodes = {node.key: node for node in (self.primary, self.replica)}
        conf = Config("", frozenset({5}), "RenderrVPN", "", "support", "", True,
                      150, 1, self.nodes, self.path, "", "")
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.telegram = Mock()
        self.bot = Bot(conf, self.store, self.telegram)
        self.addCleanup(self.bot.tasks.shutdown)
        self.now = int(time.time())

    def start_access(self, days=7):
        self.store.start_trial(123, "de", days, self.now)
        access = self.store.access(123)
        self.store.mark_applied(access)
        return self.store.access(123)

    def get(self, url):
        connection = MemorySocket(url)
        SubscriptionHandler(connection, ("127.0.0.1", 1), SimpleNamespace(database=self.path))
        headers, body = connection.output.getvalue().split(b"\r\n\r\n", 1)
        return headers, body

    def test_node_alerts_follow_transitions_with_two_failure_threshold(self):
        timeline = [True, False, False, False, True, True]
        for offset, result in enumerate(timeline):
            notify_health(self.store, self.bot, self.primary, (result, "статус"), self.now + offset * 120)
        calls = self.telegram.call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["chat_id"], 5)
        self.assertIn("Нода недоступна", calls[0].kwargs["text"])
        self.assertIn("Восстановилась", calls[1].kwargs["text"])
        self.assertEqual(self.store.node_health("de")["status"], "online")

    def test_reminder_retries_and_new_expiry_gets_a_new_reminder(self):
        self.start_access(days=1)
        first = self.store.next_reminder(self.now)
        self.telegram.call.side_effect = ServiceError("Telegram", 429)
        remind(self.store, self.bot, first, self.now)
        self.assertIsNone(self.store.next_reminder(self.now + 299))
        self.telegram.call.side_effect = None
        remind(self.store, self.bot, self.store.next_reminder(self.now + 300), self.now + 300)
        self.assertIsNone(self.store.next_reminder(self.now + 600))
        with self.store.write():
            self.store.grant(123, "new-term", 2, self.now)
        self.assertIsNone(self.store.next_reminder(self.now + DAY))
        self.assertEqual(self.store.next_reminder(self.now + 2 * DAY)["expires_at"], self.now + 3 * DAY)

    def test_unified_link_filters_unconfirmed_node_and_includes_it_after_recovery(self):
        access = self.start_access()
        token = access["sub_id"]
        self.store.set_setting("unified_url", "https://sub.test")
        self.bot.unified_available = True
        self.store.seed_placements(self.nodes, self.now)
        self.assertIn(b"404", self.get(f"/sub/{token}")[0])

        def profile(panel, placement):
            if panel.node.key == "nl" and not available:
                raise ServiceError("3x-ui", 503)
            return [f"vless://{placement['client_uuid']}@{panel.node.key}.test:443#VPN"]

        available = False
        with patch.object(Panel, "apply", autospec=True) as panel_write, \
             patch.object(Panel, "profiles", autospec=True, side_effect=profile):
            for _ in range(2):
                synchronize_placement(self.store, self.nodes, self.store.next_placement(self.now), self.now)
            headers, body = self.get(f"/sub/{token}?client=app")
            self.assertIn(b"200 OK", headers)
            self.assertIn(b"Cache-Control: private, no-store", headers)
            self.assertIn(b"@de.test", base64.b64decode(body))
            self.assertNotIn(b"@nl.test", base64.b64decode(body))
            self.assertEqual(panel_write.call_count, 1)
            self.assertIsNone(self.store.next_placement(self.now + 44))
            available = True
            now = self.now + 45
            synchronize_placement(self.store, self.nodes, self.store.next_placement(now), now)
            _, body = self.get(f"/sub/{token}")
            self.assertIn(b"@nl.test", base64.b64decode(body))
            self.assertEqual(self.bot.subscription_links(access)[0], f"https://sub.test/sub/{token}")
            self.assertIn("/de/", self.bot.subscription_links(access)[1])
            current = self.store.next_placement(now)
            self.assertIsNone(current)
            self.store.record_health("nl", False, "недоступна", now)
            self.store.record_health("nl", False, "недоступна", now + 120)
            self.assertNotIn(b"@nl.test", base64.b64decode(self.get(f"/sub/{token}")[1]))
            self.store.record_health("nl", True, "online", now + 240)
            self.assertIn(b"@nl.test", base64.b64decode(self.get(f"/sub/{token}")[1]))
            self.store.set_setting("unified_url", "")
            self.assertIn(b"404", self.get(f"/sub/{token}")[0])
            disable = self.store.next_placement(now)
            self.assertEqual(disable["node"], "nl")
            synchronize_placement(self.store, self.nodes, disable, now)
            self.assertEqual(panel_write.call_args.args[1]["desired_enabled"], 0)
            self.assertIsNone(self.store.next_placement(now))

    def test_revoke_keeps_old_links_private_and_queues_secondary_disable(self):
        access = self.start_access()
        self.store.set_setting("unified_url", "https://sub.test")
        self.store.seed_placements(self.nodes, self.now)
        for _ in range(2):
            row = self.store.next_placement(self.now)
            self.store.complete_placement(row, self.now, ["vless://profile"])
        with self.store.write():
            self.store.grant(123, "revocation", None, self.now)
        self.assertIn(b"404", self.get(f"/sub/{access['sub_id']}")[0])
        secondary = self.store.next_placement(self.now)
        self.assertEqual((secondary["node"], secondary["wanted"]), ("nl", 0))

    def test_panel_profiles_only_accept_same_uuid_from_public_subscription(self):
        access = self.start_access()
        good = f"vless://{access['client_uuid']}@de.test:443#DE"
        wrong = "vless://another-user@nl.test:443#NL"
        for wire in (good.encode(), base64.b64encode(f"{good}\n{wrong}".encode())):
            with patch("renderrvpn.services.OPENER.open", return_value=io.BytesIO(wire)) as fetch:
                self.assertEqual(Panel(self.primary).profiles(access), [good])
                self.assertEqual(urlsplit(fetch.call_args.args[0].full_url).hostname, "vpn.test")
                self.assertNotIn("Authorization", fetch.call_args.args[0].headers)
        with patch("renderrvpn.services.OPENER.open", return_value=io.BytesIO(wrong.encode())):
            with self.assertRaises(ServiceError):
                Panel(self.primary).profiles(access)

    def test_admin_enables_unified_subscription_with_a_one_time_button(self):
        self.bot.unified_available = True
        self.bot.callback(5, "a:setting:unified_url")
        self.bot.message(5, {"text": "https://sub.test", "message_id": 1})
        self.assertIsNone(self.store.setting("unified_url"))
        confirm = self.telegram.call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.bot.callback(5, confirm)
        self.assertEqual(self.store.setting("unified_url"), "https://sub.test")
        self.bot.callback(5, "a:unified_off")
        disable = self.telegram.call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.bot.callback(5, "a:settings")
        self.bot.callback(5, disable)
        self.assertEqual(self.store.setting("unified_url"), "https://sub.test")

    def test_qr_button_uses_current_private_link_and_telegram_upload(self):
        access = self.start_access()
        expected = Panel(self.primary).link(access)
        png = b"\x89PNG\r\n\x1a\n"
        with patch("renderrvpn.services.subprocess.run", return_value=SimpleNamespace(stdout=png)) as command:
            self.bot.callback(123, "qr")
            self.assertEqual(command.call_args.kwargs["input"], expected.encode())
            self.assertNotIn(expected, command.call_args.args[0])
        self.assertEqual(self.telegram.photo.call_args.args[:2], (123, png))
        self.telegram.photo.reset_mock()
        self.bot.callback(456, "qr")
        self.telegram.photo.assert_not_called()
        with patch("renderrvpn.services.response_json", return_value={"ok": True, "result": True}) as send:
            Telegram("123:fake").photo(123, png, "VPN", [[{"text": "Назад", "callback_data": "subscription"}]])
            request = send.call_args.args[1]
            self.assertTrue(request.full_url.endswith("/sendPhoto"))
            self.assertIn(b'filename="vpn.png"', request.data)
            self.assertIn(png, request.data)

    def test_invalid_qr_response_fails_safely(self):
        with patch("renderrvpn.services.subprocess.run", return_value=SimpleNamespace(stdout=b"bad")):
            with self.assertRaises(ServiceError):
                qr_png("https://vpn.test/link")


if __name__ == "__main__":
    unittest.main()
