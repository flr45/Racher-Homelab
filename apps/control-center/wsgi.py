from app import create_app
from cloudflare_extension import init_cloudflare_center
from configuration_extension import init_configuration_center
from database_browser_extension import init_database_browser
from file_browser_extension import init_file_browser
from github_extension import init_github_center
from maintenance_extension import init_maintenance
from observability_extension import init_observability
from rbac_extension import init_rbac
from security_extension import init_security

app = create_app()
init_security(app)
init_rbac(app)
init_maintenance(app)
init_github_center(app)
init_configuration_center(app)
init_observability(app)
init_database_browser(app)
init_file_browser(app)
init_cloudflare_center(app)
