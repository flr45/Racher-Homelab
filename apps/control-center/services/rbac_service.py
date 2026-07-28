ROLE_ORDER = {"anonymous": 0, "viewer": 1, "operator": 2, "admin": 3}

PERMISSIONS = {
    "anonymous": frozenset(),
    "viewer": frozenset({"system.read"}),
    "operator": frozenset(
        {
            "system.read",
            "container.start",
            "container.restart",
            "deployment.view",
            "backup.view",
        }
    ),
    "admin": frozenset({"*"}),
}


def normalize_email(value):
    return str(value or "").strip().lower()[:320]


def resolve_role(email, *, admins=(), operators=(), viewers=(), default_role="anonymous"):
    user = normalize_email(email)
    if not user:
        return "anonymous"
    admin_set = {normalize_email(item) for item in admins if normalize_email(item)}
    operator_set = {normalize_email(item) for item in operators if normalize_email(item)}
    viewer_set = {normalize_email(item) for item in viewers if normalize_email(item)}
    if user in admin_set:
        return "admin"
    if user in operator_set:
        return "operator"
    if user in viewer_set:
        return "viewer"
    return default_role if default_role in ROLE_ORDER else "anonymous"


def permissions_for(role):
    return sorted(PERMISSIONS.get(role, PERMISSIONS["anonymous"]))


def has_permission(role, permission):
    permissions = PERMISSIONS.get(role, PERMISSIONS["anonymous"])
    return "*" in permissions or permission in permissions


def role_at_least(role, minimum):
    return ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(minimum, 0)
