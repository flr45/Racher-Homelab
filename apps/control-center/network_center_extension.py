import socket

import psutil
from flask import Blueprint, jsonify

from rbac_extension import current_identity
from services.rbac_service import has_permission

network_center_blueprint = Blueprint("network_center", __name__)


def collect_network_status():
    interfaces = []
    addresses = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    for name in sorted(addresses):
        interface_stats = stats.get(name)
        interface_addresses = []
        for address in addresses[name]:
            family = str(address.family).split(".")[-1]
            if family not in {"AF_INET", "AF_INET6"}:
                continue
            interface_addresses.append(
                {
                    "family": family,
                    "address": str(address.address).split("%", 1)[0][:128],
                    "netmask": str(address.netmask or "")[:128],
                }
            )
        interfaces.append(
            {
                "name": name[:64],
                "up": bool(interface_stats and interface_stats.isup),
                "speed_mbps": int(interface_stats.speed) if interface_stats else 0,
                "mtu": int(interface_stats.mtu) if interface_stats else 0,
                "addresses": interface_addresses,
            }
        )

    listening = []
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        connections = []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        listening.append(
            {
                "address": str(connection.laddr.ip)[:128],
                "port": int(connection.laddr.port),
                "pid": int(connection.pid) if connection.pid else None,
            }
        )
    listening = sorted(listening, key=lambda item: (item["port"], item["address"]))[:200]

    return {
        "hostname": socket.gethostname()[:255],
        "interfaces": interfaces,
        "listening_ports": listening,
        "summary": {
            "interfaces": len(interfaces),
            "interfaces_up": sum(1 for item in interfaces if item["up"]),
            "listening_ports": len(listening),
        },
        "read_only": True,
    }


@network_center_blueprint.get("/api/network")
def network_status():
    identity = current_identity()
    if not has_permission(identity["role"], "system.read"):
        return jsonify({"error": "Brugeren har ikke adgang til Network Center."}), 403
    try:
        payload = collect_network_status()
    except Exception:
        payload = {
            "hostname": "unknown",
            "interfaces": [],
            "listening_ports": [],
            "summary": {"interfaces": 0, "interfaces_up": 0, "listening_ports": 0},
            "read_only": True,
            "degraded": True,
        }
    payload["actor"] = identity.get("email") or identity["role"]
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def init_network_center(app):
    app.register_blueprint(network_center_blueprint)
