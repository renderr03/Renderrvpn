import base64
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore

from .store import Store


ROUTE = re.compile(r"/sub/([a-f0-9]{32})")


class SubscriptionServer(ThreadingHTTPServer):
    def __init__(self, address, database):
        self.database = database
        self.slots = BoundedSemaphore(8)
        super().__init__(address, SubscriptionHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Subscription tokens and request details must stay out of HTTP logs.
        pass


class SubscriptionHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, b"RenderrVPN\n")
            return
        match = ROUTE.fullmatch(self.path.partition("?")[0])
        if not match:
            self.reply(404)
            return
        try:
            store = Store(self.server.database, read_only=True)
            try:
                rows = store.subscription_profiles(match[1], int(time.time())) if store.setting("unified_url") else []
            finally:
                store.db.close()
        except sqlite3.Error:
            self.reply(503)
            return
        if not rows:
            self.reply(404)
            return
        links = dict.fromkeys(link for row in rows for link in row["profiles"].splitlines())
        self.reply(200, base64.b64encode("\n".join(links).encode()), rows[0]["expires_at"])

    def reply(self, status, body=b"", expiry=None):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        if expiry is not None:
            self.send_header("Subscription-Userinfo", f"expire={expiry}")
        self.end_headers()
        self.wfile.write(body)
