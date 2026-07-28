from app import create_app
from maintenance_extension import init_maintenance

app = create_app()
init_maintenance(app)
