from flask import Flask

from config import INSTANCE_DIR, UPLOAD_DIR
from db import close_db, init_db
from extensions import login_manager
from routes.admin import register_admin_routes
from routes.auth import register_auth_routes
from routes.exports import register_export_routes
from routes.photographer import register_photographer_routes
from routes.reports import register_report_routes
from template_helpers import register_template_helpers


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "change-me-before-production"
    app.config["DATABASE"] = str(INSTANCE_DIR / "photoexpress.db")
    app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)

    login_manager.init_app(app)
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db(force=True)
        print("База данных инициализирована.")

    register_auth_routes(app)
    register_photographer_routes(app)
    register_report_routes(app)
    register_admin_routes(app)
    register_export_routes(app)
    register_template_helpers(app)

    init_db(force=False)
    return app


app = create_app()
application = app


if __name__ == "__main__":
    app.run(debug=True)
