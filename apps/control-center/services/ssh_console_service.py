import re
import subprocess

HOST_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$")
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

COMMANDS = {
    "uptime": ["uptime"],
    "disk": ["df", "-h", "--output=source,fstype,size,used,avail,pcent,target"],
    "memory": ["free", "-h"],
    "temperature": ["vcgencmd", "measure_temp"],
    "docker-ps": ["docker", "ps", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}"],
    "system-health": ["systemctl", "--failed", "--no-pager", "--plain"],
}


def parse_hosts(raw_hosts):
    hosts = {}
    for item in raw_hosts or ():
        if not isinstance(item, dict):
            continue
        host_id = str(item.get("id") or "").strip().lower()
        hostname = str(item.get("hostname") or "").strip()
        user = str(item.get("user") or "").strip()
        port = int(item.get("port") or 22)
        if (
            not HOST_PATTERN.fullmatch(host_id)
            or not HOST_PATTERN.fullmatch(hostname)
            or not USER_PATTERN.fullmatch(user)
            or port < 1
            or port > 65535
        ):
            continue
        hosts[host_id] = {
            "id": host_id,
            "hostname": hostname,
            "user": user,
            "port": port,
        }
    return hosts


def list_hosts(raw_hosts):
    return sorted(parse_hosts(raw_hosts).values(), key=lambda item: item["id"])


def execute_diagnostic(
    raw_hosts,
    host_id,
    command_id,
    *,
    known_hosts_path,
    identity_file,
    timeout_seconds=15,
    runner=subprocess.run,
):
    hosts = parse_hosts(raw_hosts)
    host = hosts.get(str(host_id or "").strip().lower())
    command = COMMANDS.get(str(command_id or "").strip().lower())
    if host is None:
        raise ValueError("Ukendt SSH-host.")
    if command is None:
        raise ValueError("Kommandoen er ikke tilladt.")
    if not known_hosts_path or not identity_file:
        raise RuntimeError("SSH-konfigurationen er ikke komplet.")

    argv = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "ConnectTimeout=5",
        "-i",
        str(identity_file),
        "-p",
        str(host["port"]),
        f'{host["user"]}@{host["hostname"]}',
        "--",
        *command,
    ]
    completed = runner(
        argv,
        capture_output=True,
        text=True,
        timeout=max(5, min(int(timeout_seconds), 60)),
        check=False,
    )
    stdout = (completed.stdout or "")[:50_000]
    stderr = (completed.stderr or "")[:10_000]
    return {
        "host": host["id"],
        "command": command_id,
        "exit_code": int(completed.returncode),
        "stdout": stdout,
        "stderr": stderr,
        "truncated": len(completed.stdout or "") > 50_000 or len(completed.stderr or "") > 10_000,
    }
