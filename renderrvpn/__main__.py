import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from threading import Event, Thread

from .bot import Bot, button
from .config import Config
from .jobs import service_notifications, service_placements
from .services import Panel, ServiceError, Telegram
from .store import Store
from .subscriptions import SubscriptionServer


LOG = logging.getLogger("renderrvpn")


def synchronize_access(store, bot, access, now):
    node = store.nodes(bot.config.nodes).get(access["node"])
    try:
        if not node:
            raise ServiceError("3x-ui", "нода отсутствует в nodes.json")
        Panel(node).apply(access)
    except ServiceError as exc:
        store.defer_access(access, now, exc)
        return
    if store.mark_applied(access) and access["desired_enabled"] and access["expires_at"] > now:
        try:
            bot.send(access["user_id"], "VPN-профиль обновлён.", [[button("Моя подписка", "subscription")]])
        except ServiceError:
            LOG.info("Не удалось отправить уведомление об обновлении профиля")


def synchronize_refund(store, telegram, payment, now):
    charge = payment["charge"]
    user = payment["user_id"]
    try:
        if telegram.call("refundStarPayment", user_id=user, telegram_payment_charge_id=charge) is not True:
            raise ServiceError("Telegram")
    except ServiceError as exc:
        reason = str(exc.reason or "").upper()
        if "CHARGE_ALREADY_REFUNDED" not in reason:
            store.defer_refund(charge, now, exc)
            return
    store.finish_refund(charge, user)


def process_pending(bot, stop):
    store = Store(bot.config.database)
    try:
        while not stop.is_set():
            try:
                now = int(time.time())
                refund = store.next_refund(now)
                if refund:
                    synchronize_refund(store, bot.telegram, refund, now)
                access = store.next_access(now)
                if access:
                    synchronize_access(store, bot, access, now)
                if not refund and not access:
                    stop.wait(1)
            except Exception:
                LOG.exception("Ошибка фоновой синхронизации")
                stop.wait(3)
    finally:
        store.db.close()


def poll(bot, stop):
    store = bot.store
    offset = int(store.setting("telegram_offset", 0))
    while not stop.is_set():
        try:
            updates = bot.telegram.call(
                "getUpdates", offset=offset, timeout=15,
                allowed_updates=["message", "callback_query", "pre_checkout_query"],
            )
            for update in updates:
                if stop.is_set():
                    break
                try:
                    bot.handle(update)
                except ServiceError as exc:
                    # Delivery failures occur after any business event was persisted.
                    LOG.warning("Ответ на событие Telegram не доставлен: %s", exc)
                offset = update["update_id"] + 1
                store.set_setting("telegram_offset", offset)
        except Exception:
            LOG.exception("Обработка событий Telegram прервана")
            stop.wait(3)


def run(config):
    config.database.parent.mkdir(parents=True, exist_ok=True)
    with config.database.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("С этой базой уже запущен процесс RenderrVPN.") from None
        store = Store(config.database)
        telegram = Telegram(config.token, config.test_mode)
        account = telegram.call("getMe")
        identity = f"{int(config.test_mode)}:{account['id']}"
        if store.setting("bot_identity") not in (None, identity):
            raise ValueError("База уже привязана к другому Telegram-боту или режиму.")
        if telegram.call("getWebhookInfo")["url"]:
            raise ValueError("У этого бота активен webhook. Используй отдельного бота для RenderrVPN.")
        store.set_setting("bot_identity", identity)
        bot = Bot(config, store, telegram)
        stop = Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        server = None
        try:
            server = SubscriptionServer((config.subscription_host, config.subscription_port), config.database)
        except OSError:
            LOG.warning("Локальный порт единой подписки недоступен. Telegram-бот запустится штатно.")
        bot.unified_available = server is not None
        workers = [
            Thread(target=task, args=(bot, stop), name=label)
            for task, label in (
                (process_pending, "sync"), (service_notifications, "notifications"),
                (service_placements, "placements"),
            )
        ]
        if server:
            workers.append(Thread(target=server.serve_forever, name="subscriptions"))
        for worker in workers:
            worker.start()
        LOG.info("Бот запущен. Нод настроено: %d.", len(bot.nodes()))
        try:
            poll(bot, stop)
        finally:
            stop.set()
            if server:
                server.shutdown()
                server.server_close()
            for worker in workers:
                worker.join()
            bot.tasks.shutdown()
            store.db.close()


def main():
    parser = argparse.ArgumentParser(description="RenderrVPN")
    parser.add_argument("--check-config", action="store_true", help="Проверить локальные настройки.")
    parser.add_argument("--check-nodes", action="store_true", help="Проверить доступ к панелям 3x-ui.")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        if args.check_nodes:
            okay = True
            nodes = config.nodes
            if config.database.is_file():
                store = Store(config.database)
                try:
                    nodes = store.nodes(nodes)
                finally:
                    store.db.close()
            for node in nodes.values():
                try:
                    healthy, status = Panel(node).health()
                except ServiceError as exc:
                    healthy, status = False, str(exc)
                print(f"{node.key}: {status}")
                okay = okay and healthy
            if not nodes:
                print("Добавь первую панель в админке бота.")
            return 0 if okay and nodes else 1
        if args.check_config:
            print(f"Настройки корректны. Нод: {len(config.nodes)}.")
        else:
            run(config)
    except (KeyError, OSError, ValueError, ServiceError) as exc:
        if isinstance(exc, OSError):
            LOG.error("Не удалось прочитать настройки или открыть базу: %s", exc.strerror)
        elif isinstance(exc, KeyError):
            LOG.error("В настройках отсутствует обязательное поле: %s", exc)
        else:
            LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
