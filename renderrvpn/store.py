import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .config import Node


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
CREATE TABLE IF NOT EXISTS nodes (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_sales (
    node TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_health (
    node TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    failures INTEGER NOT NULL,
    checked_at INTEGER NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expiry_reminders (
    user_id INTEGER NOT NULL REFERENCES access(user_id),
    expires_at INTEGER NOT NULL,
    done INTEGER NOT NULL,
    retry_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, expires_at)
);
CREATE TABLE IF NOT EXISTS placements (
    user_id INTEGER NOT NULL REFERENCES access(user_id),
    node TEXT NOT NULL,
    panel_email TEXT NOT NULL,
    client_uuid TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    confirmed_revision INTEGER NOT NULL DEFAULT 0,
    confirmed_enabled INTEGER NOT NULL DEFAULT 0,
    profiles TEXT NOT NULL DEFAULT '',
    refresh_at INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    retry_revision INTEGER NOT NULL DEFAULT 0,
    retry_enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, node)
);
CREATE INDEX IF NOT EXISTS active_expiry ON access(desired_enabled, expires_at);
"""


class Store:
    def __init__(self, path, *, read_only=False):
        source = Path(path).resolve().as_uri() + "?mode=ro" if read_only else path
        self.db = sqlite3.connect(source, uri=read_only, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        if not read_only:
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

    def nodes(self, defaults):
        result = dict(defaults)
        for row in self.db.execute("SELECT data FROM nodes ORDER BY rowid"):
            node = Node.read(json.loads(row["data"]))
            result[node.key] = node
        return result

    def save_node(self, node, defaults, admin_id, *, rotate=False):
        with self.write():
            known = self.nodes(defaults)
            if rotate:
                if node.key not in known:
                    raise ValueError("Локация больше не найдена.")
            else:
                if len(known) >= 20:
                    raise ValueError("Можно подключить до 20 локаций.")
                if any((item.api_url, item.inbound_id) == (node.api_url, node.inbound_id) for item in known.values()):
                    raise ValueError("Этот inbound уже подключён.")
            targets = [replace(item, token=node.token) for item in known.values()
                       if item.api_url == node.api_url] if rotate else [node]
            self.db.executemany(
                "INSERT INTO nodes VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET data = excluded.data",
                ((item.key, json.dumps(asdict(item))) for item in targets),
            )
            self.log_action(admin_id, "смена токена панели" if rotate else "подключение ноды", {"node": node.key})

    def log_action(self, admin_id, action, args):
        self.db.execute(
            "INSERT INTO audit(admin_id, action, args, created_at) VALUES (?, ?, ?, ?)",
            (admin_id, action, json.dumps(args), int(time.time())),
        )

    def sales_open(self, node):
        row = self.one("SELECT enabled FROM node_sales WHERE node = ?", node)
        return row is None or bool(row["enabled"])

    def access(self, user_id):
        return self.one("SELECT * FROM access WHERE user_id = ?", user_id)

    def node_health(self, key):
        return self.one("SELECT * FROM node_health WHERE node = ?", key)

    def record_health(self, key, ok, detail, now):
        with self.write():
            last = self.node_health(key)
            before = last["status"] if last else "unknown"
            failures = 0 if ok else (last["failures"] if last else 0) + 1
            status = "online" if ok else ("offline" if failures >= 2 else before)
            self.db.execute("INSERT OR REPLACE INTO node_health VALUES (?, ?, ?, ?, ?)",
                            (key, status, failures, now, detail))
        if status != before and (status == "offline" or before == "offline"):
            return status
        return None

    def next_reminder(self, now):
        return self.one(
            """SELECT a.user_id, a.expires_at FROM access a
               LEFT JOIN expiry_reminders r ON a.user_id = r.user_id AND a.expires_at = r.expires_at
               WHERE a.desired_enabled = 1 AND a.expires_at BETWEEN ? AND ?
                 AND (a.applied_revision > 0 OR EXISTS (
                     SELECT 1 FROM placements p WHERE p.user_id = a.user_id
                       AND p.confirmed_enabled = 1 AND p.confirmed_revision = a.revision AND p.profiles != ''
                 ))
                 AND (r.done IS NULL OR (r.done = 0 AND r.retry_at <= ?))
               ORDER BY a.expires_at LIMIT 1""",
            now + 1, now + DAY, now,
        )

    def mark_reminder(self, reminder, done, retry_at=0):
        self.db.execute("INSERT OR REPLACE INTO expiry_reminders VALUES (?, ?, ?, ?)",
                        (reminder["user_id"], reminder["expires_at"], done, retry_at))

    def seed_placements(self, nodes, now):
        for key in nodes:
            opened = self.sales_open(key)
            candidates = self.db.execute(
                """SELECT a.* FROM access a WHERE a.desired_enabled = 1 AND a.expires_at > ?
                   AND (a.node = ? OR ?) AND NOT EXISTS (
                       SELECT 1 FROM placements p WHERE p.node = ? AND p.user_id = a.user_id
                   ) LIMIT 20""",
                (now, key, opened, key),
            ).fetchall()
            identities = []
            for a in candidates:
                primary = a["node"] == key
                identities.append((
                    a["user_id"], key,
                    a["panel_email"] if primary else f"rvpn-{a['user_id']}-{secrets.token_hex(5)}",
                    a["client_uuid"] if primary else str(uuid.uuid4()),
                    a["sub_id"] if primary else secrets.token_hex(16),
                ))
            if identities:
                self.db.executemany(
                    """INSERT OR IGNORE INTO placements(user_id, node, panel_email, client_uuid, sub_id)
                       VALUES (?, ?, ?, ?, ?)""", identities,
                )

    def next_placement(self, now):
        return self.db.execute(
            """WITH desired AS (
                   SELECT p.*, a.node AS primary_node, a.expires_at, a.revision, a.applied_revision,
                          (a.desired_enabled = 1 AND a.expires_at > :now AND EXISTS (
                             SELECT 1 FROM settings WHERE key = 'unified_url' AND value != ''
                          )) AS wanted
                   FROM placements p JOIN access a ON a.user_id = p.user_id
               )
               SELECT * FROM desired
               WHERE (retry_at <= :now OR retry_revision != revision OR retry_enabled != wanted)
                 AND (node != primary_node OR (wanted AND applied_revision = revision))
                 AND (revision != confirmed_revision OR wanted != confirmed_enabled
                      OR (wanted AND refresh_at <= :now))
               ORDER BY CASE WHEN wanted THEN 1 ELSE 0 END,
                        CASE WHEN revision != confirmed_revision THEN 0 ELSE 1 END, refresh_at
               LIMIT 1""", {"now": now},
        ).fetchone()

    def complete_placement(self, placement, now, profiles):
        self.db.execute(
            """UPDATE placements SET confirmed_revision = ?, confirmed_enabled = ?, profiles = ?,
               refresh_at = ?, retry_at = 0 WHERE user_id = ? AND node = ?""",
            (placement["revision"], placement["wanted"], "\n".join(profiles), now + 900,
             placement["user_id"], placement["node"]),
        )

    def defer_placement(self, placement, now):
        self.db.execute(
            """UPDATE placements SET profiles = '', retry_at = ?, retry_revision = ?, retry_enabled = ?
               WHERE user_id = ? AND node = ?""",
            (now + 45, placement["revision"], placement["wanted"], placement["user_id"], placement["node"]),
        )

    def subscription_profiles(self, sub_id, now):
        rows = self.db.execute(
            """SELECT p.node, p.profiles, a.expires_at FROM access a
               JOIN placements p ON p.user_id = a.user_id
               LEFT JOIN node_health h ON h.node = p.node
               WHERE a.sub_id = ? AND a.desired_enabled = 1 AND a.expires_at > ?
                 AND EXISTS (SELECT 1 FROM settings WHERE key = 'unified_url' AND value != '')
                 AND p.confirmed_revision = a.revision AND p.confirmed_enabled = 1 AND p.profiles != ''
                 AND p.refresh_at + 900 > ? AND COALESCE(h.status, '') != 'offline'
               ORDER BY p.node != a.node, p.node""",
            (sub_id, now, now),
        ).fetchall()
        return rows

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
