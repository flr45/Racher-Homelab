from app_core import *  # noqa: F401,F403
from training import TrainingStore
from training_routes import register_training_routes


training = TrainingStore(DB_PATH, routing, adaptive)
register_training_routes(app, storage, training, auth_required)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
