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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        PRAGMA foreign_keys = ON;

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

    if db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == 0:
        seed = [
            ("Politiet lavede en ______ omkring gerningsstedet.", "afspærring", "Politi", 2),
            ("Betjenten talte med et ______ til ulykken.", "vidne", "Politi", 1),
            ("Patruljen kørte hurtigt frem til ______.", "stedet", "Politi", 1),
            ("Føreren blev bedt om at vise sit ______.", "kørekort", "Politi", 2),
            ("Politiet sikrede ______ på gerningsstedet.", "spor", "Politi", 1),
            ("Efter ulykken skrev betjenten en ______.", "rapport", "Politi", 2),
            ("Politiet undersøgte ______ fra overvågningskameraet.", "optagelser", "Politi", 3),
            ("Den mistænkte blev kørt til ______.", "stationen", "Politi", 2),
            ("Brandfolkene rullede en ______ ud fra bilen.", "brandslange", "Brand", 2),
            ("Røgdykkerne gik ind i den brændende ______.", "bygning", "Brand", 2),
            ("Brandvæsenet begyndte en ______ af beboerne.", "evakuering", "Brand", 3),
            ("Holdlederen gav en kort ______ til mandskabet.", "briefing", "Brand", 3),
            ("Ambulancen kørte med blå ______.", "blink", "Ambulance", 1),
            ("Redderne undersøgte den ______ person.", "tilskadekomne", "Ambulance", 3),
            ("Patienten blev lagt på en ______.", "båre", "Ambulance", 1),
            ("Ambulancepersonalet målte patientens ______.", "blodtryk", "Ambulance", 2),
            ("Ved en ulykke skal området først gøres ______.", "sikkert", "Redningsberedskab", 2),
            ("Indsatslederen skabte hurtigt et ______ over situationen.", "overblik", "Redningsberedskab", 2),
            ("Ved større hændelser kan flere ______ arbejde sammen.", "myndigheder", "Redningsberedskab", 3),
            ("Mandskabet blev sendt frem med deres ______.", "udstyr", "Redningsberedskab", 1),
        ]
        db.executemany(
            "INSERT INTO exercises (sentence,answer,category,difficulty) VALUES (?,?,?,?)",
            seed,
        )

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
        user = get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not user or not user["is_admin"]:
            flash("Du har ikke adgang til adminområdet.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    user = None
    if session.get("user_id"):
        user = get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    return {"current_user": user}


@app.route("/")
def index():
    return redirect(url_for("dashboard" if session.get("user_id") else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
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
    stats = db.execute(
        """
        SELECT COUNT(*) AS total_attempts,
               SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct_attempts,
               COUNT(DISTINCT CASE WHEN is_correct=1 THEN exercise_id END) AS learned
        FROM attempts WHERE user_id=?
        """,
        (uid,),
    ).fetchone()
    total = stats["total_attempts"] or 0
    correct = stats["correct_attempts"] or 0
    accuracy = round(correct / total * 100) if total else 0
    problem_words = db.execute(
        """
        SELECT e.answer,e.category,m.wrong_count,m.correct_count
        FROM mastery m JOIN exercises e ON e.id=m.exercise_id
        WHERE m.user_id=? AND m.wrong_count>m.correct_count
        ORDER BY (m.wrong_count-m.correct_count) DESC,m.last_seen DESC LIMIT 6
        """,
        (uid,),
    ).fetchall()
    return render_template(
        "dashboard.html",
        total=total,
        correct=correct,
        accuracy=accuracy,
        learned=stats["learned"] or 0,
        problem_words=problem_words,
    )


def choose_exercise(user_id: int):
    rows = get_db().execute(
        """
        SELECT e.*,COALESCE(m.correct_count,0) correct_count,
               COALESCE(m.wrong_count,0) wrong_count,m.last_seen
        FROM exercises e
        LEFT JOIN mastery m ON m.exercise_id=e.id AND m.user_id=?
        WHERE e.active=1
        """,
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    weighted = []
    for row in rows:
        weight = 3
        if row["wrong_count"] > row["correct_count"]:
            weight += 6
        if row["correct_count"] == 0:
            weight += 4
        if row["correct_count"] >= 3 and row["wrong_count"] == 0:
            weight = 1
        weighted.extend([row] * weight)
    return random.choice(weighted)


@app.route("/practice")
@login_required
def practice():
    exercise = choose_exercise(session["user_id"])
    if not exercise:
        flash("Der er endnu ingen aktive øvelser.", "error")
        return redirect(url_for("dashboard"))
    session["exercise_id"] = exercise["id"]
    session["attempt_no"] = 0
    return render_template("practice.html", exercise=exercise)


@app.route("/check", methods=["POST"])
@login_required
def check_answer():
    db = get_db()
    exercise_id = session.get("exercise_id")
    if not exercise_id:
        return {"ok": False, "message": "Ingen aktiv øvelse."}, 400
    exercise = db.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
    if not exercise:
        return {"ok": False, "message": "Øvelsen findes ikke."}, 404

    typed = request.form.get("answer", "").strip()
    is_correct = typed.casefold() == exercise["answer"].strip().casefold()
    session["attempt_no"] = session.get("attempt_no", 0) + 1
    attempt_no = session["attempt_no"]

    db.execute(
        "INSERT INTO attempts (user_id,exercise_id,typed_answer,is_correct,attempt_no) VALUES (?,?,?,?,?)",
        (session["user_id"], exercise_id, typed, int(is_correct), attempt_no),
    )
    db.execute(
        """
        INSERT INTO mastery (user_id,exercise_id,correct_count,wrong_count,last_seen)
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
        return {"ok": True, "correct": True, "message": "Korrekt! Flot arbejde."}

    answer = exercise["answer"]
    if attempt_no == 2:
        hint = f"Hint: Ordet starter med “{answer[0]}”."
    elif attempt_no >= 3:
        masked = " ".join(ch if i in (0, len(answer)-1) else "_" for i, ch in enumerate(answer))
        hint = f"Ekstra hint: {masked}"
    else:
        hint = "Prøv igen."
    return {"ok": True, "correct": False, "message": hint, "attempt_no": attempt_no}


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    exercises = db.execute("SELECT * FROM exercises ORDER BY category,difficulty,id DESC").fetchall()
    users = db.execute(
        """
        SELECT u.*,COUNT(a.id) attempts,
               SUM(CASE WHEN a.is_correct=1 THEN 1 ELSE 0 END) correct
        FROM users u LEFT JOIN attempts a ON a.user_id=u.id
        GROUP BY u.id ORDER BY u.display_name
        """
    ).fetchall()
    return render_template("admin.html", exercises=exercises, users=users)


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
    if not sentence or "______" not in sentence or not answer:
        flash("Sætningen skal indeholde ______ og have et svarord.", "error")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO exercises (sentence,answer,category,difficulty) VALUES (?,?,?,?)",
            (sentence, answer, category, difficulty),
        )
        db.commit()
        flash("Øvelsen blev oprettet.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/exercise/<int:exercise_id>/toggle", methods=["POST"])
@admin_required
def toggle_exercise(exercise_id):
    db = get_db()
    db.execute("UPDATE exercises SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (exercise_id,))
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
        flash("Brugernavn skal være mindst 3 tegn, navn skal udfyldes og adgangskoden mindst 6 tegn.", "error")
        return redirect(url_for("admin"))
    try:
        db = get_db()
        db.execute(
            "INSERT INTO users (username,password_hash,display_name,is_admin) VALUES (?,?,?,0)",
            (username, generate_password_hash(password), display_name),
        )
        db.commit()
        flash(f"Elevkontoen {display_name} blev oprettet.", "success")
    except sqlite3.IntegrityError:
        flash("Brugernavnet findes allerede.", "error")
    return redirect(url_for("admin"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=False)
