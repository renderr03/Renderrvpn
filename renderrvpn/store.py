import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager


DAY = 86_400
PLAN_DAYS = 30
DATABASE = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access (
    user_id INTEGER PRIMARY KEY,
    node TEXT NOT NULL,
    panel_email TEXT NOT NULL UNIQUE,
    client_uuid TEXT NOT NULL UNIQUE,
    sub_id TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL DEFAULT 0,
    desired_enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    applied_revision INTEGER NOT NULL DEFAULT 0,
    next_try INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES access(user_id),
    amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    accepted_checkout TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    charge TEXT PRIMARY KEY,
    invoice TEXT NOT NULL REFERENCES invoices(token),
    user_id INTEGER NOT NULL REFERENCES access(user_id),
    amount INTEGER NOT NULL,
    paid_at INTEGER NOT NULL,
    refund_state TEXT NOT NULL DEFAULT 'paid',
    next_try INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS entitlements (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES access(user_id),
    source TEXT NOT NULL UNIQUE,
    granted_at INTEGER NOT NULL,
    days INTEGER
);
CREATE INDEX IF NOT EXISTS entitlement_user ON entitlements(user_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    token TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_sales (
    node TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(DATABASE)

    @contextmanager
    def write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", key)
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    def sales_open(self, node):
        row = self.one("SELECT enabled FROM node_sales WHERE node = ?", node)
        return row is None or bool(row["enabled"])

    def access(self, user_id):
        return self.one("SELECT * FROM access WHERE user_id = ?", user_id)

    def ensure_access(self, user_id, node):
        row = self.access(user_id)
        if row is not None:
            if row["node"] != node:
                raise ValueError("За этой подпиской уже закреплена другая локация.")
            return
        self.db.execute(
            "INSERT INTO access(user_id, node, panel_email, client_uuid, sub_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, node, f"rvpn-{user_id}-{secrets.token_hex(5)}", str(uuid.uuid4()), secrets.token_hex(16)),
        )

    def create_invoice(self, user_id, node, amount, now):
        token = "rv:" + secrets.token_urlsafe(20)
        with self.write():
            self.ensure_access(user_id, node)
            self.db.execute(
                "INSERT INTO invoices(token, user_id, amount, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, amount, now),
            )
        return token

    def accept_checkout(self, token, user_id, currency, amount, checkout_id, now):
        with self.write():
            invoice = self.one("SELECT * FROM invoices WHERE token = ?", token)
            if (
                not invoice or invoice["user_id"] != user_id or currency != "XTR"
                or invoice["amount"] != amount or invoice["created_at"] + 1800 < now
                or invoice["accepted_checkout"] not in (None, checkout_id)
                or self.one("SELECT 1 FROM payments WHERE invoice = ?", token)
            ):
                return False
            self.db.execute("UPDATE invoices SET accepted_checkout = ? WHERE token = ?", (checkout_id, token))
            return True

    def grant(self, user_id, source, days, now):
        self.db.execute(
            "INSERT INTO entitlements(user_id, source, granted_at, days) VALUES (?, ?, ?, ?)",
            (user_id, source, now, days),
        )
        self.refresh_access(user_id)

    def refresh_access(self, user_id):
        end = 0
        events = self.db.execute(
            """SELECT e.granted_at, e.days FROM entitlements e
               LEFT JOIN payments p ON e.source = 'pay:' || p.charge
               WHERE e.user_id = ? AND (p.refund_state IS NULL OR p.refund_state != 'refunded')
               ORDER BY e.id""",
            (user_id,),
        )
        for event in events:
            end = 0 if event["days"] is None else max(end, event["granted_at"]) + DAY * event["days"]
        row = self.access(user_id)
        if row["expires_at"] != end or bool(row["desired_enabled"]) != bool(end):
            self.db.execute(
                """UPDATE access SET expires_at = ?, desired_enabled = ?, revision = revision + 1,
                   next_try = 0, last_error = NULL WHERE user_id = ?""",
                (end, bool(end), user_id),
            )

    def start_trial(self, user_id, node, days, now):
        with self.write():
            if self.one("SELECT 1 FROM entitlements WHERE user_id = ?", user_id):
                raise ValueError("Пробный доступ уже использован или у тебя уже была подписка.")
            self.ensure_access(user_id, node)
            self.grant(user_id, f"trial:{user_id}", days, now)

    def record_payment(self, token, user_id, currency, amount, charge, now):
        with self.write():
            invoice = self.one("SELECT * FROM invoices WHERE token = ?", token)
            if (
                not invoice or invoice["user_id"] != user_id or currency != "XTR"
                or invoice["amount"] != amount or not invoice["accepted_checkout"]
                or not charge
            ):
                return "mismatch"
            if self.one("SELECT 1 FROM payments WHERE charge = ?", charge):
                return "duplicate"
            self.db.execute(
                """INSERT INTO payments(charge, invoice, user_id, amount, paid_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (charge, token, user_id, amount, now),
            )
            self.grant(user_id, f"pay:{charge}", PLAN_DAYS, now)
            return "paid"

    def payment(self, charge):
        return self.one("SELECT * FROM payments WHERE charge = ?", charge)

    def finish_refund(self, charge, user_id):
        with self.write():
            payment = self.payment(charge)
            if not payment or payment["user_id"] != user_id or payment["refund_state"] == "refunded":
                return False
            self.db.execute(
                "UPDATE payments SET refund_state = 'refunded', last_error = NULL WHERE charge = ?",
                (charge,),
            )
            self.refresh_access(user_id)
            return True

    def next_refund(self, now):
        return self.one(
            "SELECT * FROM payments WHERE refund_state = 'refund_pending' AND next_try <= ? ORDER BY paid_at LIMIT 1",
            now,
        )

    def defer_refund(self, charge, now, error):
        self.db.execute(
            "UPDATE payments SET next_try = ?, last_error = ? WHERE charge = ? AND refund_state = 'refund_pending'",
            (now + 60, str(error), charge),
        )

    def next_access(self, now):
        return self.one(
            """SELECT * FROM access WHERE revision > applied_revision AND next_try <= ?
               ORDER BY next_try, user_id LIMIT 1""",
            now,
        )

    def mark_applied(self, access):
        result = self.db.execute(
            """UPDATE access SET applied_revision = ?, last_error = NULL
               WHERE user_id = ? AND revision = ? AND applied_revision < ?""",
            (access["revision"], access["user_id"], access["revision"], access["revision"]),
        )
        return result.rowcount == 1

    def defer_access(self, access, now, error):
        self.db.execute(
            "UPDATE access SET next_try = ?, last_error = ? WHERE user_id = ? AND revision = ?",
            (now + 30, str(error), access["user_id"], access["revision"]),
        )

    def approval(self, admin_id, action, args):
        token = secrets.token_urlsafe(15)
        now = int(time.time())
        with self.write():
            self.db.execute("DELETE FROM approvals WHERE expires_at < ?", (now,))
            self.db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
                (token, admin_id, action, json.dumps(args), now + 900),
            )
        return token

    def cancel(self, actor, token):
        result = self.db.execute("DELETE FROM approvals WHERE admin_id = ? AND token = ?", (actor, token))
        return result.rowcount == 1

    def confirm(self, actor, token):
        now = int(time.time())
        with self.write():
            item = self.one(
                "SELECT * FROM approvals WHERE token = ? AND admin_id = ? AND expires_at >= ?",
                token, actor, now,
            )
            if not item:
                return "Подтверждение устарело или уже использовано."
            action = item["action"]
            args = json.loads(item["args"])
            if action in ("grant", "revoke"):
                user = args["user"]
                if action == "grant":
                    self.ensure_access(user, args["node"])
                elif not self.access(user):
                    raise ValueError("Подписка не найдена.")
                self.grant(user, f"admin:{token}", args.get("days"), now)
            elif action == "retry":
                changed = self.db.execute(
                    """UPDATE access SET revision = revision + 1, next_try = 0, last_error = NULL
                       WHERE user_id = ? AND revision > 0""",
                    (args["user"],),
                )
                if not changed.rowcount:
                    raise ValueError("У клиента нет операции для повторной выдачи.")
            elif action == "price":
                self.set_setting("price", args["amount"])
            elif action == "trial":
                self.set_setting("trial", args["days"])
            elif action == "sales":
                self.db.execute(
                    "INSERT OR REPLACE INTO node_sales VALUES (?, ?)",
                    (args["node"], args["enabled"]),
                )
            elif action == "refund":
                changed = self.db.execute(
                    """UPDATE payments SET refund_state = 'refund_pending', next_try = 0, last_error = NULL
                       WHERE charge = ? AND refund_state != 'refunded'""",
                    (args["charge"],),
                )
                if not changed.rowcount:
                    raise ValueError("Платёж уже возвращён или не найден.")
            else:
                raise ValueError("Неизвестная операция.")
            self.db.execute("DELETE FROM approvals WHERE token = ?", (token,))
            self.db.execute(
                "INSERT INTO audit(admin_id, action, args, created_at) VALUES (?, ?, ?, ?)",
                (actor, action, item["args"], now),
            )
            return "Изменение сохранено. Операции панели и возвраты выполняются в фоне."
