from services.database_service import open_database
from services.migration_service import run_migrations


def init_migrations(app):
    connection = open_database(
        app.config["DATA_ROOT"],
        app.config["DATABASE_PATH"],
    )
    try:
        run_migrations(connection)
    finally:
        connection.close()
