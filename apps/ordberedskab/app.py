from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ORDBEREDSKAB_DB", BASE_DIR / "ordberedskab.db"))
SEED_DIR = BASE_DIR / "seed"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")

DIFFICULTY_LABELS = {
    1: "Let",
    2: "Normal",
    3: "Svær",
    4: "Meget svær",
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def load_seed_exercises(db: sqlite3.Connection) -> int:
    inserted = 0
    if not SEED_DIR.exists():
        return inserted

    category_by_file = {
        "politi.tsv": "Politi",
        "brand.tsv": "Brand",
        "ambulance.tsv": "Ambulance",
        "redningsberedskab.tsv": "Redningsberedskab",
    }

    for path in sorted(SEED_DIR.glob("*.tsv")):
        category = category_by_file.get(path.name)
        if not category:
            continue

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.split("\t", 2)
            if len(parts) != 3:
                continue
            raw_difficulty, answer, sentence = parts
            try:
                difficulty = int(raw_difficulty)
            except ValueError:
                continue
            answer = answer.strip()
            sentence = sentence.strip()
            if difficulty not in DIFFICULTY_LABELS or not answer or sentence.count("______") != 1:
                continue

            exists = db.execute(
                "SELECT 1 FROM exercises WHERE sentence=? AND answer=? LIMIT 1",
                (sentence, answer),
            ).fetchone()
            if exists:
                continue

            db.execute(
                "INSERT INTO exercises (sentence,answer,category,difficulty) VALUES (?,?,?,?)",
                (sentence, answer, category, difficulty),
            )
            inserted += 1

    return inserted


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sentence TEXT NOT NULL,
            answer TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Politi',
            difficulty INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            exercise_id INTEGER NOT NULL,
            typed_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS mastery (
            user_id INTEGER NOT NULL,
            exercise_id INTEGER NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            wrong_count INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            PRIMARY KEY(user_id, exercise_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            difficulty INTEGER NOT NULL DEFAULT 2,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS completions (
            user_id INTEGER NOT NULL,
            exercise_id INTEGER NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('correct','revealed')),
            attempts INTEGER NOT NULL DEFAULT 1,
            completed_at TEXT NOT NULL,
            PRIMARY KEY(user_id, exercise_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_exercises_level_active
          ON exercises(difficulty, active);

        CREATE INDEX IF NOT EXISTS idx_attempts_user_exercise
          ON attempts(user_id, exercise_id);

        CREATE INDEX IF NOT EXISTS idx_completions_user_time
          ON completions(user_id, completed_at DESC);
        """
    )

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        admin_password = os.environ.get("ORDBEREDSKAB_ADMIN_PASSWORD", "admin123")
        student_password = os.environ.get("ORDBEREDSKAB_STUDENT_PASSWORD", "elev123")
        db.execute(
            "INSERT INTO users (username,password_hash,display_name,is_admin) VALUES (?,?,?,1)",
            ("admin", generate_password_hash(admin_password), "Administrator"),
        )
        db.execute(
            "INSERT INTO users (username,password_hash,display_name,is_admin) VALUES (?,?,?,0)",
            ("elev", generate_password_hash(student_password), "Elev"),
        )

    db.execute(
        """
        INSERT OR IGNORE INTO user_preferences (user_id,difficulty)
        SELECT id,2 FROM users
        """
    )

    db.execute(
        """
        INSERT OR IGNORE INTO completions (user_id,exercise_id,result,attempts,completed_at)
        SELECT m.user_id,m.exercise_id,'correct',1,COALESCE(m.last_seen,CURRENT_TIMESTAMP)
        FROM mastery m
        WHERE m.correct_count > 0
        """
    )

    load_seed_exercises(db)
    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = get_db().execute(
            "SELECT * FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
        if not user or not user["is_admin"]:
            flash("Du har ikke adgang til adminområdet.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    user = None
    if session.get("user_id"):
        user = get_db().execute(
            "SELECT * FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
    return {
        "current_user": user,
        "difficulty_labels": DIFFICULTY_LABELS,
    }


def get_user_difficulty(user_id: int) -> int:
    row = get_db().execute(
        "SELECT difficulty FROM user_preferences WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row:
        return max(1, min(4, int(row["difficulty"])))
    get_db().execute(
        "INSERT OR IGNORE INTO user_preferences (user_id,difficulty) VALUES (?,2)",
        (user_id,),
    )
    get_db().commit()
    return 2


def _unseen_rows(user_id: int, difficulty: int, exclude_id: int | None = None):
    sql = """
        SELECT e.*,
               COALESCE(m.correct_count,0) AS correct_count,
               COALESCE(m.wrong_count,0) AS wrong_count
        FROM exercises e
        LEFT JOIN completions c
          ON c.exercise_id=e.id AND c.user_id=?
        LEFT JOIN mastery m
          ON m.exercise_id=e.id AND m.user_id=?
        WHERE e.active=1
          AND e.difficulty=?
          AND c.exercise_id IS NULL
    """
    params: list[object] = [user_id, user_id, difficulty]
    if exclude_id:
        sql += " AND e.id<>?"
        params.append(exclude_id)
    return get_db().execute(sql, params).fetchall()


def _review_exercise(user_id: int, difficulty: int, exclude_id: int | None = None):
    sql = """
        SELECT e.*,
               COALESCE(m.correct_count,0) AS correct_count,
               COALESCE(m.wrong_count,0) AS wrong_count
        FROM exercises e
        LEFT JOIN mastery m
          ON m.exercise_id=e.id AND m.user_id=?
        WHERE e.active=1 AND e.difficulty=?
    """
    params: list[object] = [user_id, difficulty]
    if exclude_id:
        sql += " AND e.id<>?"
        params.append(exclude_id)
    sql += """
        ORDER BY
          (COALESCE(m.wrong_count,0)-COALESCE(m.correct_count,0)) DESC,
          COALESCE(m.last_seen,'') ASC,
          RANDOM()
        LIMIT 1
    """
    row = get_db().execute(sql, params).fetchone()
    if row:
        return row
    if exclude_id:
        return get_db().execute(
            "SELECT * FROM exercises WHERE active=1 AND difficulty=? ORDER BY RANDOM() LIMIT 1",
            (difficulty,),
        ).fetchone()
    return None


def _insert_generated_exercises(items: list[dict]) -> int:
    db = get_db()
    inserted = 0
    for item in items:
        exists = db.execute(
            "SELECT 1 FROM exercises WHERE sentence=? AND answer=? LIMIT 1",
            (item["sentence"], item["answer"]),
        ).fetchone()
        if exists:
            continue
        db.execute(
            "INSERT INTO exercises (sentence,answer,category,difficulty) VALUES (?,?,?,?)",
            (
                item["sentence"],
                item["answer"],
                item["category"],
                item["difficulty"],
            ),
        )
        inserted += 1
    db.commit()
    return inserted


def generate_new_batch(
    difficulty: int,
    *,
    count: int | None = None,
    exhausted_user_id: int | None = None,
    exclude_id: int | None = None,
) -> int:
    from ai_generator import GENERATION_LOCK, generate_exercises

    difficulty = max(1, min(4, int(difficulty)))
    with GENERATION_LOCK:
        if exhausted_user_id is not None:
            if _unseen_rows(exhausted_user_id, difficulty, exclude_id):
                return 0

        existing = get_db().execute(
            """
            SELECT sentence,answer FROM exercises
            WHERE difficulty=?
            ORDER BY id DESC
            LIMIT 160
            """,
            (difficulty,),
        ).fetchall()
        avoid_sentences = [row["sentence"] for row in existing]
        avoid_answers = [row["answer"] for row in existing]

        generated = generate_exercises(
            difficulty=difficulty,
            count=count,
            avoid_sentences=avoid_sentences,
            avoid_answers=avoid_answers,
        )
        return _insert_generated_exercises(generated)


def choose_exercise(
    user_id: int,
    *,
    exclude_id: int | None = None,
    allow_generation: bool = True,
):
    difficulty = get_user_difficulty(user_id)
    unseen = _unseen_rows(user_id, difficulty, exclude_id)
    if unseen:
        weighted = []
        for row in unseen:
            weight = 5 if row["correct_count"] == 0 and row["wrong_count"] == 0 else 3
            if row["wrong_count"] > 0:
                weight += 2
            weighted.extend([row] * weight)
        return random.choice(weighted)

    if allow_generation:
        try:
            inserted = generate_new_batch(
                difficulty,
                exhausted_user_id=user_id,
                exclude_id=exclude_id,
            )
            if inserted:
                unseen = _unseen_rows(user_id, difficulty, exclude_id)
                if unseen:
                    return random.choice(unseen)
        except Exception:
            app.logger.exception(
                "Automatisk generering af nye øvelser fejlede for user_id=%s level=%s",
                user_id,
                difficulty,
            )

    return _review_exercise(user_id, difficulty, exclude_id)


def mark_completion(user_id: int, exercise_id: int, result: str, attempts: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    db = get_db()
    db.execute(
        """
        INSERT INTO completions (user_id,exercise_id,result,attempts,completed_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id,exercise_id) DO UPDATE SET
          result=CASE
            WHEN completions.result='correct' OR excluded.result='correct'
              THEN 'correct'
            ELSE 'revealed'
          END,
          attempts=excluded.attempts,
          completed_at=excluded.completed_at
        """,
        (user_id, exercise_id, result, attempts, now),
    )
    db.commit()


@app.route("/")
def index():
    return redirect(url_for("dashboard" if session.get("user_id") else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Forkert brugernavn eller adgangskode.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = session["user_id"]
    difficulty = get_user_difficulty(uid)

    stats = db.execute(
        """
        SELECT COUNT(*) AS total_attempts,
               SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_attempts
        FROM attempts WHERE user_id=?
        """,
        (uid,),
    ).fetchone()
    total = stats["total_attempts"] or 0
    correct = stats["correct_attempts"] or 0
    accuracy = round(correct / total * 100) if total else 0

    completed_count = db.execute(
        "SELECT COUNT(*) FROM completions WHERE user_id=?",
        (uid,),
    ).fetchone()[0]
    learned = db.execute(
        "SELECT COUNT(*) FROM completions WHERE user_id=? AND result='correct'",
        (uid,),
    ).fetchone()[0]

    available_level = db.execute(
        "SELECT COUNT(*) FROM exercises WHERE active=1 AND difficulty=?",
        (difficulty,),
    ).fetchone()[0]
    completed_level = db.execute(
        """
        SELECT COUNT(*)
        FROM completions c JOIN exercises e ON e.id=c.exercise_id
        WHERE c.user_id=? AND e.active=1 AND e.difficulty=?
        """,
        (uid, difficulty),
    ).fetchone()[0]
    bank_total = db.execute(
        "SELECT COUNT(*) FROM exercises WHERE active=1"
    ).fetchone()[0]

    problem_words = db.execute(
        """
        SELECT e.answer,e.category,e.difficulty,m.wrong_count,m.correct_count
        FROM mastery m JOIN exercises e ON e.id=m.exercise_id
        WHERE m.user_id=? AND m.wrong_count>m.correct_count
        ORDER BY (m.wrong_count-m.correct_count) DESC,m.last_seen DESC
        LIMIT 6
        """,
        (uid,),
    ).fetchall()

    completed_recent = db.execute(
        """
        SELECT e.sentence,e.answer,e.category,e.difficulty,
               c.result,c.attempts,c.completed_at
        FROM completions c
        JOIN exercises e ON e.id=c.exercise_id
        WHERE c.user_id=?
        ORDER BY c.completed_at DESC
        LIMIT 10
        """,
        (uid,),
    ).fetchall()

    return render_template(
        "dashboard.html",
        total=total,
        correct=correct,
        accuracy=accuracy,
        learned=learned,
        completed_count=completed_count,
        problem_words=problem_words,
        completed_recent=completed_recent,
        difficulty=difficulty,
        available_level=available_level,
        completed_level=completed_level,
        remaining_level=max(0, available_level - completed_level),
        bank_total=bank_total,
    )


@app.route("/difficulty", methods=["POST"])
@login_required
def set_difficulty():
    try:
        difficulty = int(request.form.get("difficulty", "2"))
    except ValueError:
        difficulty = 2
    difficulty = max(1, min(4, difficulty))
    db = get_db()
    db.execute(
        """
        INSERT INTO user_preferences (user_id,difficulty)
        VALUES (?,?)
        ON CONFLICT(user_id) DO UPDATE SET difficulty=excluded.difficulty
        """,
        (session["user_id"], difficulty),
    )
    db.commit()
    flash(
        f"Sværhedsgraden er sat til niveau {difficulty} – {DIFFICULTY_LABELS[difficulty]}.",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route("/completed")
@login_required
def completed():
    rows = get_db().execute(
        """
        SELECT e.sentence,e.answer,e.category,e.difficulty,
               c.result,c.attempts,c.completed_at
        FROM completions c
        JOIN exercises e ON e.id=c.exercise_id
        WHERE c.user_id=?
        ORDER BY c.completed_at DESC
        """,
        (session["user_id"],),
    ).fetchall()
    return render_template("completed.html", completed_rows=rows)


@app.route("/practice")
@login_required
def practice():
    requested_id = request.args.get("exercise_id", type=int)
    difficulty = get_user_difficulty(session["user_id"])
    exercise = None

    if requested_id:
        exercise = get_db().execute(
            """
            SELECT * FROM exercises
            WHERE id=? AND active=1 AND difficulty=?
            """,
            (requested_id, difficulty),
        ).fetchone()

    if not exercise:
        exercise = choose_exercise(session["user_id"])

    if not exercise:
        flash("Der er endnu ingen aktive øvelser på dette niveau.", "error")
        return redirect(url_for("dashboard"))

    session["exercise_id"] = exercise["id"]
    session["attempt_no"] = 0
    return render_template("practice.html", exercise=exercise)


@app.route("/api/prepare-next", methods=["POST"])
@login_required
def prepare_next():
    current_id = session.get("exercise_id")
    exercise = choose_exercise(
        session["user_id"],
        exclude_id=current_id,
        allow_generation=True,
    )
    if not exercise:
        return {"ok": False, "message": "Ingen næste øvelse kunne findes."}, 404

    return {
        "ok": True,
        "exercise_id": exercise["id"],
        "next_url": url_for("practice", exercise_id=exercise["id"]),
        "audio": {
            "normal": url_for(
                "tts.exercise_audio",
                exercise_id=exercise["id"],
                mode="normal",
            ),
            "slow": url_for(
                "tts.exercise_audio",
                exercise_id=exercise["id"],
                mode="slow",
            ),
        },
    }


@app.route("/check", methods=["POST"])
@login_required
def check_answer():
    db = get_db()
    exercise_id = session.get("exercise_id")
    if not exercise_id:
        return {"ok": False, "message": "Ingen aktiv øvelse."}, 400

    exercise = db.execute(
        "SELECT * FROM exercises WHERE id=?", (exercise_id,)
    ).fetchone()
    if not exercise:
        return {"ok": False, "message": "Øvelsen findes ikke."}, 404

    typed = request.form.get("answer", "").strip()
    is_correct = typed.casefold() == exercise["answer"].strip().casefold()
    session["attempt_no"] = session.get("attempt_no", 0) + 1
    attempt_no = session["attempt_no"]

    db.execute(
        """
        INSERT INTO attempts
          (user_id,exercise_id,typed_answer,is_correct,attempt_no)
        VALUES (?,?,?,?,?)
        """,
        (session["user_id"], exercise_id, typed, int(is_correct), attempt_no),
    )
    db.execute(
        """
        INSERT INTO mastery
          (user_id,exercise_id,correct_count,wrong_count,last_seen)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id,exercise_id) DO UPDATE SET
          correct_count=correct_count+excluded.correct_count,
          wrong_count=wrong_count+excluded.wrong_count,
          last_seen=excluded.last_seen
        """,
        (
            session["user_id"],
            exercise_id,
            1 if is_correct else 0,
            0 if is_correct else 1,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()

    if is_correct:
        mark_completion(session["user_id"], exercise_id, "correct", attempt_no)
        return {
            "ok": True,
            "correct": True,
            "completed": True,
            "message": "Korrekt! Flot arbejde.",
        }

    if attempt_no >= 2:
        mark_completion(session["user_id"], exercise_id, "revealed", attempt_no)
        return {
            "ok": True,
            "correct": False,
            "completed": True,
            "revealed": True,
            "answer": exercise["answer"],
            "message": f"Det rigtige ord er: {exercise['answer']}",
        }

    return {
        "ok": True,
        "correct": False,
        "completed": False,
        "message": "Ikke helt. Prøv én gang mere.",
        "attempt_no": attempt_no,
    }


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    exercises = db.execute(
        "SELECT * FROM exercises ORDER BY category,difficulty,id DESC"
    ).fetchall()
    users = db.execute(
        """
        SELECT u.*,COUNT(a.id) attempts,
               SUM(CASE WHEN a.is_correct=1 THEN 1 ELSE 0 END) correct
        FROM users u LEFT JOIN attempts a ON a.user_id=u.id
        GROUP BY u.id ORDER BY u.display_name
        """
    ).fetchall()
    level_counts = {
        level: db.execute(
            "SELECT COUNT(*) FROM exercises WHERE active=1 AND difficulty=?",
            (level,),
        ).fetchone()[0]
        for level in DIFFICULTY_LABELS
    }
    return render_template(
        "admin.html",
        exercises=exercises,
        users=users,
        level_counts=level_counts,
    )


@app.route("/admin/exercise/add", methods=["POST"])
@admin_required
def add_exercise():
    sentence = request.form.get("sentence", "").strip()
    answer = request.form.get("answer", "").strip()
    category = request.form.get("category", "Politi").strip() or "Politi"
    try:
        difficulty = max(1, min(4, int(request.form.get("difficulty", "1"))))
    except ValueError:
        difficulty = 1

    if not sentence or sentence.count("______") != 1 or not answer:
        flash("Sætningen skal indeholde præcis én ______ og have et svarord.", "error")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO exercises (sentence,answer,category,difficulty) VALUES (?,?,?,?)",
            (sentence, answer, category, difficulty),
        )
        db.commit()
        flash("Øvelsen blev oprettet.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/generate", methods=["POST"])
@admin_required
def admin_generate():
    try:
        difficulty = max(1, min(4, int(request.form.get("difficulty", "2"))))
        count = max(5, min(40, int(request.form.get("count", "20"))))
    except ValueError:
        difficulty, count = 2, 20

    try:
        inserted = generate_new_batch(difficulty, count=count)
        flash(f"AI oprettede {inserted} nye øvelser på niveau {difficulty}.", "success")
    except Exception:
        app.logger.exception("Manuel AI-generering fejlede")
        flash("AI kunne ikke generere nye øvelser lige nu.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/exercise/<int:exercise_id>/toggle", methods=["POST"])
@admin_required
def toggle_exercise(exercise_id):
    db = get_db()
    db.execute(
        """
        UPDATE exercises
        SET active=CASE WHEN active=1 THEN 0 ELSE 1 END
        WHERE id=?
        """,
        (exercise_id,),
    )
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/exercise/<int:exercise_id>/delete", methods=["POST"])
@admin_required
def delete_exercise(exercise_id):
    db = get_db()
    db.execute("DELETE FROM exercises WHERE id=?", (exercise_id,))
    db.commit()
    flash("Øvelsen blev slettet.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/user/add", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username", "").strip().lower()
    display_name = request.form.get("display_name", "").strip()
    password = request.form.get("password", "")
    if len(username) < 3 or len(password) < 6 or not display_name:
        flash(
            "Brugernavn skal være mindst 3 tegn, navn skal udfyldes og adgangskoden mindst 6 tegn.",
            "error",
        )
        return redirect(url_for("admin"))

    try:
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO users (username,password_hash,display_name,is_admin)
            VALUES (?,?,?,0)
            """,
            (username, generate_password_hash(password), display_name),
        )
        db.execute(
            "INSERT OR IGNORE INTO user_preferences (user_id,difficulty) VALUES (?,2)",
            (cursor.lastrowid,),
        )
        db.commit()
        flash(f"Elevkontoen {display_name} blev oprettet.", "success")
    except sqlite3.IntegrityError:
        flash("Brugernavnet findes allerede.", "error")
    return redirect(url_for("admin"))


init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5050")),
        debug=False,
    )
