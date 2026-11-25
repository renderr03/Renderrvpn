import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ServiceError(Exception):
    def __init__(self, service, code=None, reason=None):
        super().__init__(f"{service}: {code or 'ошибка ответа'}")
        self.code = code
        self.reason = reason


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)
MAX_RESPONSE = 8 * 1024 * 1024


def request_json(service, url, *, payload=None, token=None, timeout=8):
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=body, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        with exc:
            raw = exc.read(2048)
        reason = None
        if service == "Telegram":
            try:
                reason = json.loads(raw).get("description")
            except (ValueError, AttributeError):
                pass
        raise ServiceError(service, exc.code, reason) from None
    except (URLError, OSError, ValueError, HTTPException):
        raise ServiceError(service, "нет соединения") from None
    try:
        data = json.loads(raw) if len(raw) <= MAX_RESPONSE else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ServiceError(service)
    return data


class Telegram:
    def __init__(self, token, test_mode=False):
        prefix = "test/" if test_mode else ""
        self.base = f"https://api.telegram.org/bot{token}/{prefix}"

    def call(self, method, **payload):
        timeout = payload.get("timeout", 0) + 6 if method == "getUpdates" else 8
        data = request_json("Telegram", self.base + method, payload=payload, timeout=timeout)
        if data.get("ok") is not True:
            raise ServiceError("Telegram", data.get("error_code"), data.get("description"))
        return data["result"]


class Panel:
    def __init__(self, node):
        self.node = node

    def call(self, path, payload=None):
        data = request_json(
            "3x-ui", f"{self.node.api_url}/panel/api/{path}",
            payload=payload, token=self.node.token,
        )
        if data.get("success") is not True:
            raise ServiceError("3x-ui", "панель отклонила операцию")
        return data.get("obj")

    def health(self):
        status = self.call("server/status")
        inbound = self.call(f"inbounds/get/{self.node.inbound_id}")
        if not isinstance(inbound, dict) or inbound.get("protocol") != "vless":
            return False, "inbound должен быть VLESS"
        if inbound.get("enable") is not True:
            return False, "inbound выключен"
        if inbound.get("nodeId"):
            return False, "укажи API самой ноды, которая обслуживает этот inbound"
        xray = status.get("xray") if isinstance(status, dict) else None
        if not isinstance(xray, dict) or not xray.get("state"):
            return False, "панель отвечает, статус Xray недоступен"
        if str(xray["state"]).lower() != "running":
            return False, "панель отвечает, Xray не запущен"
        return True, "панель отвечает, Xray запущен и VLESS включён"

    def apply(self, access):
        user_id = access["user_id"]
        email = access["panel_email"]
        records = self.call(f"clients/get/tgId/{user_id}")
        if not isinstance(records, list):
            raise ServiceError("3x-ui")
        record = next((r for r in records if r.get("client", {}).get("email") == email), None)
        enabled = bool(access["desired_enabled"])
        end_ms = access["expires_at"] * 1000
        if record is None:
            if not enabled:
                return
            inbound = self.call(f"inbounds/get/{self.node.inbound_id}")
            if not isinstance(inbound, dict) or inbound.get("protocol") != "vless" or inbound.get("enable") is not True:
                raise ServiceError("3x-ui", "проверь inbound VLESS")
            if inbound.get("nodeId"):
                raise ServiceError("3x-ui", "укажи API панели на самой ноде")
            result = self.call("clients/add", {
                "client": {
                    "id": access["client_uuid"], "email": email, "subId": access["sub_id"],
                    "tgId": user_id, "enable": True, "expiryTime": end_ms,
                    "flow": self.node.flow, "comment": "RenderrVPN",
                },
                "inboundIds": [self.node.inbound_id],
            })
        else:
            client = record["client"]
            if (
                client.get("subId") != access["sub_id"] or client.get("uuid") != access["client_uuid"]
                or record.get("inboundIds") != [self.node.inbound_id]
            ):
                raise ServiceError("3x-ui", "исходные параметры клиента изменились")
            if client.get("enable") is enabled and client.get("expiryTime") == end_ms:
                return
            # The GET record has a numeric database id while the write body has the VLESS UUID.
            fields = (
                "email", "subId", "flow", "security", "limitIp", "limitHwid", "totalGB",
                "tgId", "group", "comment", "reset", "resetDay", "resetWeekday",
                "resetMax", "trafficReset", "trafficResetDay", "clientHostRuleIds",
            )
            payload = {key: client[key] for key in fields if key in client}
            payload.update(id=client["uuid"], enable=enabled, expiryTime=end_ms)
            result = self.call("clients/update/" + quote(email, safe=""), payload)
        if isinstance(result, dict) and result.get("nodePending"):
            raise ServiceError("3x-ui", "изменение ожидает связанную ноду")

    def link(self, access):
        return f"{self.node.subscription_url}/{quote(access['sub_id'], safe='')}"
