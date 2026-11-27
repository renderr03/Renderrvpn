import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .menu import button
from .services import Panel, ServiceError
from .store import Store


LOG = logging.getLogger(__name__)
HEALTH_INTERVAL = 120


def check_node(node):
    try:
        return Panel(node).health()
    except ServiceError as exc:
        return False, str(exc)


def notify_health(store, bot, node, result, now):
    ok, detail = result
    transition = store.record_health(node.key, ok, detail, now)
    if not transition:
        return
    status = "Восстановилась" if transition == "online" else "Нода недоступна"
    for admin in bot.config.admins:
        try:
            bot.send(admin, f"{status} · {node.label}\n{detail}",
                     [[button("Открыть ноду", f"a:node:{node.key}")]])
        except ServiceError:
            LOG.info("Не удалось доставить уведомление о состоянии ноды")


def remind(store, bot, reminder, now):
    current = store.access(reminder["user_id"])
    if not current or not current["desired_enabled"] or current["expires_at"] != reminder["expires_at"]:
        return
    try:
        bot.send(
            reminder["user_id"], "До окончания подписки осталось меньше суток.",
            [[button("Продлить", "shop"), button("Моя подписка", "subscription")]],
        )
    except ServiceError as exc:
        store.mark_reminder(reminder, exc.code in (400, 403), now + 300)
    else:
        store.mark_reminder(reminder, True)


def service_notifications(bot, stop):
    store = Store(bot.config.database)
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="node-health")
    try:
        while not stop.is_set():
            try:
                now = int(time.time())
                nodes = store.nodes(bot.config.nodes)
                due = [n for n in nodes.values() if
                       (last := store.node_health(n.key)) is None or last["checked_at"] + HEALTH_INTERVAL <= now]
                probes = [(n, pool.submit(check_node, n)) for n in due]
                for node, probe in probes:
                    if stop.is_set():
                        break
                    notify_health(store, bot, node, probe.result(), int(time.time()))
                for _ in range(4):
                    now = int(time.time())
                    reminder = None if stop.is_set() else store.next_reminder(now)
                    if not reminder:
                        break
                    remind(store, bot, reminder, now)
            except Exception:
                LOG.exception("Ошибка проверки нод или отправки напоминаний")
            stop.wait(10)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        store.db.close()


def synchronize_placement(store, nodes, placement, now):
    node = nodes.get(placement["node"])
    try:
        if node is None:
            raise ServiceError("3x-ui", "нода отсутствует")
        panel = Panel(node)
        changed = (placement["revision"] != placement["confirmed_revision"]
                   or placement["wanted"] != placement["confirmed_enabled"])
        if changed and placement["node"] != placement["primary_node"]:
            panel.apply(dict(placement, desired_enabled=placement["wanted"]))
        profiles = panel.profiles(placement) if placement["wanted"] else []
    except ServiceError:
        store.defer_placement(placement, now)
    else:
        store.complete_placement(placement, now, profiles)


def service_placements(bot, stop):
    store = Store(bot.config.database)
    seed_at = 0
    nodes = {}
    try:
        while not stop.is_set():
            try:
                now = int(time.time())
                if now >= seed_at:
                    enabled = store.setting("unified_url")
                    nodes = store.nodes(bot.config.nodes)
                    if enabled:
                        store.seed_placements(nodes, now)
                    seed_at = now + 5
                placement = store.next_placement(now)
                if placement:
                    synchronize_placement(store, nodes, placement, now)
                else:
                    stop.wait(1)
            except Exception:
                LOG.exception("Ошибка синхронизации единой подписки")
                stop.wait(3)
    finally:
        store.db.close()
