import json

from .services import ServiceError, request_json


ACTIONS = [
    "help", "nodes", "find", "payments", "grant", "revoke", "retry",
    "price", "trial", "sales", "refund",
]
FIELDS = {
    "action": {"type": "string", "enum": ACTIONS},
    "user": {"type": ["integer", "null"]},
    "days": {"type": ["integer", "null"]},
    "amount": {"type": ["integer", "null"]},
    "node": {"type": ["string", "null"]},
    "enabled": {"type": ["boolean", "null"]},
    "payment": {"type": ["integer", "null"]},
}


def interpret(config, message):
    nodes = ", ".join(f"{n.key} ({n.label})" for n in config.nodes.values())
    instruction = (
        "Преобразуй запрос владельца VPN-магазина в одну операцию. "
        "Используй help, если запрос неясен, действий несколько или их нет в списке. "
        "nodes — состояние нод; find — найти подписку по числовому Telegram ID user; "
        "payments — показать последние платежи; grant — выдать или продлить подписку "
        "(user, days, node при выборе ноды, иначе null); revoke — отключить доступ (user); "
        "retry — повторить синхронизацию (user); price — цена на 30 дней в Stars (amount); "
        "trial — пробный период (days, 0 для отключения); sales — продажи на ноде "
        "(node и enabled); refund — вернуть платёж по номеру из списка (payment). "
        "Не угадывай идентификаторы и значения. Неизвестные поля оставь null. "
        f"Список ключей и названий нод: {nodes}."
    )
    data = request_json(
        "OpenAI", "https://api.openai.com/v1/chat/completions", token=config.openai_key,
        timeout=30,
        payload={
            "model": config.openai_model,
            "store": False,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": message},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vpn_admin_action",
                    "strict": True,
                    "schema": {
                        "type": "object", "properties": FIELDS,
                        "required": list(FIELDS), "additionalProperties": False,
                    },
                },
            },
        },
    )
    try:
        result = json.loads(data["choices"][0]["message"]["content"])
        if not isinstance(result, dict) or result.get("action") not in ACTIONS:
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise ServiceError("OpenAI", "помощник не распознал операцию") from None
    return result
