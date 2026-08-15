import threading

from gateway import FileTailSource


# app_core owns the shared source object, but the production entrypoint needs to
# install the source-mode gate before the tail thread can consume a live line.
# Temporarily suppress FileTailSource.start() during app_core import, then restore
# the method and start the source only after selected_pdl_line is installed.
_original_file_tail_start = FileTailSource.start
FileTailSource.start = lambda self: None
try:
    from app_core import *  # noqa: F401,F403
finally:
    FileTailSource.start = _original_file_tail_start

from training import TrainingStore
from training_routes import register_training_routes


# The first-admin setup route checks whether any users exist before creating the
# initial administrator. Gunicorn runs this appliance with one worker and several
# threads, so serialize that whole check-and-create flow to prevent two concurrent
# setup requests from both becoming administrators.
_setup_lock = threading.Lock()
_original_setup_view = app.view_functions["setup"]


def serialized_setup(*args, **kwargs):
    with _setup_lock:
        return _original_setup_view(*args, **kwargs)


app.view_functions["setup"] = serialized_setup


# The PDL tailer deliberately keeps following the logfile in every mode so its
# file offset stays current. Only forward decoded lines into the live ingest path
# when PDL is the selected source. This prevents simulator mode from generating
# real pager alarms and avoids replaying a backlog when switching back to PDL.
def selected_pdl_line(line: str) -> None:
    if setting("source_mode", "mock") == "pdl-file":
        on_pdl_line(line)


source.on_line = selected_pdl_line
source.start()

training = TrainingStore(DB_PATH, routing, adaptive)
register_training_routes(app, storage, training, auth_required)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")), debug=False)
