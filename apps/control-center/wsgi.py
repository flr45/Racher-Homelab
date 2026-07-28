from app import create_app
from configuration_extension import init_configuration_center
from github_extension import init_github_center
from maintenance_extension import init_maintenance
from observability_extension import init_observability

app = create_app()
init_maintenance(app)
init_github_center(app)
init_configuration_center(app)
init_observability(app)
