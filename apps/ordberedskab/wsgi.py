from app import app
from tts import tts_bp

app.register_blueprint(tts_bp)
