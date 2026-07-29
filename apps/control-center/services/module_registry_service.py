MODULES = (
    {
        "id": "dashboard",
        "name": "Dashboard",
        "description": "Live systemstatus, metrics og hændelser.",
        "href": "/",
        "category": "overview",
        "permission": "system.read",
        "status_endpoint": "/api/status",
    },
    {
        "id": "operations",
        "name": "Operations Timeline",
        "description": "Samlet audit-, event-, notification- og deploymenthistorik.",
        "href": "/api/operations-timeline",
        "category": "operations",
        "permission": "system.read",
        "status_endpoint": "/api/operations-timeline?limit=1",
    },
    {
        "id": "docker",
        "name": "Docker Center",
        "description": "Containere, images, netværk, volumes og sikker oprydning.",
        "href": "/api/docker-center",
        "category": "operations",
        "permission": "system.read",
        "status_endpoint": "/api/docker-center",
    },
    {
        "id": "deployments",
        "name": "Deployment Center",
        "description": "Inventory, rollout, healthcheck og automatisk rollback.",
        "href": "/api/deployment-actions",
        "category": "operations",
        "permission": "deployment.view",
        "status_endpoint": "/api/deployment-actions",
    },
    {
        "id": "restore",
        "name": "Restore Center",
        "description": "Validering og staging af verificerede backups.",
        "href": "/api/restore",
        "category": "recovery",
        "permission": "backup.view",
        "status_endpoint": "/api/restore",
    },
    {
        "id": "database",
        "name": "Database Browser",
        "description": "Read-only tabel-, schema- og rækkevisning.",
        "href": "/database",
        "category": "data",
        "permission": "system.read",
        "status_endpoint": "/api/database",
    },
    {
        "id": "files",
        "name": "File Browser",
        "description": "Sikker read-only adgang til godkendte mapper.",
        "href": "/files",
        "category": "data",
        "permission": "system.read",
        "status_endpoint": "/api/files",
    },
    {
        "id": "cloudflare",
        "name": "Cloudflare Center",
        "description": "Tunnels, DNS, Access og domænestatus.",
        "href": "/api/cloudflare",
        "category": "integrations",
        "permission": "system.read",
        "status_endpoint": "/api/cloudflare",
    },
    {
        "id": "github",
        "name": "GitHub Center",
        "description": "Read-only repository-, branch- og pull request-status.",
        "href": "/api/github",
        "category": "integrations",
        "permission": "system.read",
        "status_endpoint": "/api/github",
    },
    {
        "id": "plugins",
        "name": "Plugin Center",
        "description": "Deklarative plugins, permissions og kompatibilitet.",
        "href": "/api/plugins",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/plugins",
    },
    {
        "id": "readiness",
        "name": "Readiness Center",
        "description": "Installationskontrol for Docker, data, backup, database og integrationer.",
        "href": "/api/readiness",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/readiness",
    },
    {
        "id": "hardware",
        "name": "Node & Hardware Center",
        "description": "Pi-model, kernel, storage, temperatur, throttling, netværk og Docker-host.",
        "href": "/api/node-hardware",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/node-hardware",
    },
    {
        "id": "network",
        "name": "Network Center",
        "description": "Interfaces, IP-adresser, linkstatus og lyttende porte.",
        "href": "/api/network",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/network",
    },
    {
        "id": "storage",
        "name": "Storage Center",
        "description": "Mounts, filsystemer, kapacitet og read-only storage-advarsler.",
        "href": "/api/storage",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/storage",
    },
    {
        "id": "services",
        "name": "Service Health Center",
        "description": "Read-only oversigt over fejlede systemd-services.",
        "href": "/api/service-health",
        "category": "operations",
        "permission": "system.read",
        "status_endpoint": "/api/service-health",
    },
    {
        "id": "updates",
        "name": "Update Center",
        "description": "Read-only oversigt over tilgængelige systempakkeopdateringer.",
        "href": "/api/updates",
        "category": "operations",
        "permission": "system.read",
        "status_endpoint": "/api/updates",
    },
    {
        "id": "configuration",
        "name": "Configuration Center",
        "description": "Secrets- og environment-inventory uden værdilæk.",
        "href": "/api/configuration",
        "category": "platform",
        "permission": "system.read",
        "status_endpoint": "/api/configuration",
    },
    {
        "id": "support",
        "name": "Support Bundle Center",
        "description": "Sanitiseret admin-only diagnosepakke uden credentials eller logs.",
        "href": "/api/support-bundle",
        "category": "administration",
        "permission": "support.export",
        "status_endpoint": "/api/support-bundle",
    },
    {
        "id": "ssh",
        "name": "SSH Console",
        "description": "Allowlistede diagnosekommandoer uden fri shell.",
        "href": "/api/ssh-console",
        "category": "administration",
        "permission": "ssh.manage",
        "status_endpoint": "/api/ssh-console",
    },
)

CATEGORY_ORDER = (
    "overview",
    "operations",
    "recovery",
    "data",
    "integrations",
    "platform",
    "administration",
)


def visible_modules(role, permission_check):
    visible = []
    for module in MODULES:
        if permission_check(role, module["permission"]):
            visible.append(dict(module))
    return visible


def grouped_modules(modules):
    groups = []
    for category in CATEGORY_ORDER:
        items = [item for item in modules if item["category"] == category]
        if items:
            groups.append({"category": category, "modules": items})
    return groups
