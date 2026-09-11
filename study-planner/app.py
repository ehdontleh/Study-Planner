import os
import re
import sqlite3
import json
from datetime import datetime, date, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get("STUDY_DB_PATH", "database.db")

# If DATABASE_URL is set (e.g. on Render, pointing at a Postgres instance),
# use Postgres so data survives redeploys. Otherwise fall back to a local
# SQLite file — used for local development and the test suite.
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    # Some hosts (Render, Heroku) hand out the old "postgres://" scheme;
    # psycopg2/SQLAlchemy expect "postgresql://".
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access your planner."


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
#
# The rest of the app is written against a simple `connection.execute(query,
# params)` interface (SQLite's convenience API). Connection wraps a psycopg2
# connection so the same calling code works unchanged against Postgres too —
# only this section knows which database is actually in use.

class Connection:
    def __init__(self, raw, is_pg):
        self._raw = raw
        self._is_pg = is_pg

    def execute(self, query, params=()):
        if self._is_pg:
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(query.replace("?", "%s"), params)
            return cur
        return self._raw.execute(query, params)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def get_db():
    if DATABASE_URL:
        raw = psycopg2.connect(DATABASE_URL)
        return Connection(raw, is_pg=True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Connection(raw, is_pg=False)


def insert_and_get_id(connection, query, params):
    """Runs an INSERT and returns the new row's id, on either backend."""
    if DATABASE_URL:
        cursor = connection.execute(query + " RETURNING id", params)
        return cursor.fetchone()["id"]
    cursor = connection.execute(query, params)
    return cursor.lastrowid


def init_db():
    connection = get_db()
    if DATABASE_URL:
        cur = connection._raw.cursor()
        cur.execute("SELECT to_regclass('public.users')")
        exists = cur.fetchone()[0] is not None
        if not exists:
            with open(os.path.join(os.path.dirname(__file__), "schema_postgres.sql")) as f:
                cur.execute(f.read())
            connection.commit()
    else:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if table is None:
            with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
                connection._raw.executescript(f.read())
            connection.commit()
    connection.close()


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.password_hash = row["password_hash"]


@login_manager.user_loader
def load_user(user_id):
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    connection.close()
    return User(row) if row else None


def task_owned_by_current_user(connection, task_id):
    row = connection.execute(
        """
        SELECT subjects.user_id AS user_id
        FROM tasks
        JOIN topics ON tasks.topic_id = topics.id
        JOIN subjects ON topics.subject_id = subjects.id
        WHERE tasks.id = ?
        """,
        (task_id,)
    ).fetchone()
    return row is not None and row["user_id"] == current_user.id


def topic_owned_by_current_user(connection, topic_id):
    row = connection.execute(
        """
        SELECT subjects.user_id AS user_id
        FROM topics
        JOIN subjects ON topics.subject_id = subjects.id
        WHERE topics.id = ?
        """,
        (topic_id,)
    ).fetchone()
    return row is not None and row["user_id"] == current_user.id


def subject_owned_by_current_user(connection, subject_id):
    row = connection.execute(
        "SELECT user_id FROM subjects WHERE id = ?", (subject_id,)
    ).fetchone()
    return row is not None and row["user_id"] == current_user.id


def session_owned_by_current_user(connection, session_id):
    row = connection.execute(
        """
        SELECT subjects.user_id AS user_id
        FROM study_sessions
        JOIN tasks ON study_sessions.task_id = tasks.id
        JOIN topics ON tasks.topic_id = topics.id
        JOIN subjects ON topics.subject_id = subjects.id
        WHERE study_sessions.id = ?
        """,
        (session_id,)
    ).fetchone()
    return row is not None and row["user_id"] == current_user.id


# ---------------------------------------------------------------------------
# Auth (V6)
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        confirm = request.form["confirm_password"]

        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for("register"))

        if password != confirm:
            flash("Passwords do not match.")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("register"))

        connection = get_db()
        existing = connection.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()

        if existing:
            connection.close()
            flash("That username is already taken.")
            return redirect(url_for("register"))

        connection.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), datetime.now().isoformat())
        )
        connection.commit()
        connection.close()

        flash("Account created — please log in.")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        connection = get_db()
        row = connection.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        connection.close()

        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row))
            return redirect(url_for("index"))

        flash("Invalid username or password.")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# V1 — Core system
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    connection = get_db()

    subjects = connection.execute(
        "SELECT * FROM subjects WHERE user_id = ?", (current_user.id,)
    ).fetchall()
    subjects = [dict(s) for s in subjects]
    subject_ids = [s["id"] for s in subjects]

    if subject_ids:
        placeholders = ",".join("?" * len(subject_ids))
        topics = connection.execute(
            f"SELECT * FROM topics WHERE subject_id IN ({placeholders})",
            subject_ids
        ).fetchall()
    else:
        topics = []
    topics = [dict(t) for t in topics]
    topic_ids = [t["id"] for t in topics]

    if topic_ids:
        placeholders = ",".join("?" * len(topic_ids))
        tasks = connection.execute(
            f"""
            SELECT tasks.*, topics.name AS topic_name, subjects.name AS subject_name
            FROM tasks
            JOIN topics ON tasks.topic_id = topics.id
            JOIN subjects ON topics.subject_id = subjects.id
            WHERE tasks.topic_id IN ({placeholders})
            """,
            topic_ids
        ).fetchall()
    else:
        tasks = []
    tasks = [dict(t) for t in tasks]

    for task in tasks:
        result = connection.execute(
            """
            SELECT
                COUNT(status) AS total_prerequisites,
                COUNT(CASE WHEN status = 'Completed' THEN 1 END) AS completed_prerequisites
            FROM prerequisites
            JOIN tasks ON prerequisites.prerequisite_id = tasks.id
            WHERE task_id = ?
            """,
            (task["id"],)
        ).fetchone()
        task["available"] = (
            result["total_prerequisites"] == result["completed_prerequisites"]
        )

    for topic in topics:
        topic_tasks = [t for t in tasks if t["topic_id"] == topic["id"]]
        topic["has_tasks"] = len(topic_tasks) > 0

    for subject in subjects:
        subject_tasks = [
            t for t in tasks
            if any(
                topic["id"] == t["topic_id"] and topic["subject_id"] == subject["id"]
                for topic in topics
            )
        ]
        subject["total_tasks"] = len(subject_tasks)
        subject["has_tasks"] = subject["total_tasks"] > 0
        subject["completed_tasks"] = sum(
            1 for t in subject_tasks if t["status"] == "Completed"
        )
        subject["progress"] = (
            (subject["completed_tasks"] / subject["total_tasks"]) * 100
            if subject["total_tasks"] > 0 else 0
        )

    connection.close()

    total_tasks = len(tasks)
    completed_tasks = sum(1 for t in tasks if t["status"] == "Completed")
    progress = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0

    return render_template(
        "index.html",
        tasks=tasks,
        subjects=subjects,
        topics=topics,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        progress=progress
    )


@app.route("/add_subject", methods=["POST"])
@login_required
def add_subject():
    name = request.form["name"].strip()
    if not name:
        flash("Subject name can't be empty.")
        return redirect(url_for("index"))

    connection = get_db()
    connection.execute(
        "INSERT INTO subjects (name, user_id) VALUES (?, ?)",
        (name, current_user.id)
    )
    connection.commit()
    connection.close()
    return redirect(url_for("index"))


@app.route("/add_topic", methods=["POST"])
@login_required
def add_topic():
    name = request.form["name"].strip()
    subject_id = request.form["subject_id"]

    connection = get_db()

    if not subject_owned_by_current_user(connection, subject_id):
        connection.close()
        flash("You don't have access to that subject.")
        return redirect(url_for("index"))

    connection.execute(
        "INSERT INTO topics (name, subject_id) VALUES (?, ?)",
        (name, subject_id)
    )
    connection.commit()
    connection.close()
    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
@login_required
def add():
    topic_id = request.form["topic_id"]
    title = request.form["title"]
    estimated_minutes = request.form["estimated_minutes"]
    resource_url = request.form["resource_url"]
    description = request.form["description"]
    difficulty = request.form["difficulty"]
    priority = request.form["priority"]
    deadline = request.form["deadline"]
    prerequisites = request.form.getlist("prerequisites")

    connection = get_db()

    if not topic_owned_by_current_user(connection, topic_id):
        connection.close()
        flash("You don't have access to that topic.")
        return redirect(url_for("index"))

    new_task_id = insert_and_get_id(
        connection,
        """
        INSERT INTO tasks
            (title, estimated_minutes, resource_url, description,
             difficulty, priority, deadline, topic_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, estimated_minutes, resource_url, description,
         difficulty, priority, deadline, topic_id)
    )

    for prerequisite_id in prerequisites:
        if task_owned_by_current_user(connection, prerequisite_id):
            connection.execute(
                "INSERT INTO prerequisites (task_id, prerequisite_id) VALUES (?, ?)",
                (new_task_id, prerequisite_id)
            )

    connection.commit()
    connection.close()
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>")
@login_required
def edit(task_id):
    connection = get_db()

    if not task_owned_by_current_user(connection, task_id):
        connection.close()
        flash("You don't have access to that task.")
        return redirect(url_for("index"))

    task = connection.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    connection.close()
    return render_template("edit.html", task=task)


@app.route("/edit", methods=["POST"])
@login_required
def edit_task():
    task_id = request.form["task_id"]
    connection = get_db()

    if not task_owned_by_current_user(connection, task_id):
        connection.close()
        flash("You don't have access to that task.")
        return redirect(url_for("index"))

    connection.execute(
        """
        UPDATE tasks
        SET title = ?, description = ?, estimated_minutes = ?,
            priority = ?, difficulty = ?, deadline = ?, resource_url = ?
        WHERE id = ?
        """,
        (
            request.form["title"], request.form["description"],
            request.form["estimated_minutes"], request.form["priority"],
            request.form["difficulty"], request.form["deadline"],
            request.form["resource_url"], task_id
        )
    )
    connection.commit()
    connection.close()
    return redirect(url_for("index"))


@app.route("/delete", methods=["POST"])
@login_required
def delete():
    task_id = request.form["task_id"]
    redirect_to = "plan" if request.form.get("next") == "plan" else "index"
    connection = get_db()

    if not task_owned_by_current_user(connection, task_id):
        connection.close()
        flash("You don't have access to that task.")
        return redirect(url_for(redirect_to))

    connection.execute(
        "DELETE FROM prerequisites WHERE task_id = ? OR prerequisite_id = ?",
        (task_id, task_id)
    )
    connection.execute("DELETE FROM study_sessions WHERE task_id = ?", (task_id,))
    connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    connection.commit()
    connection.close()
    flash("Task deleted." if redirect_to == "index" else "Task deleted. Rebuild your plan to refresh this list.")
    return redirect(url_for(redirect_to))


@app.route("/complete", methods=["POST"])
@login_required
def complete():
    task_id = request.form["task_id"]
    time_studied = request.form["time_studied"]
    redirect_to = "plan" if request.form.get("next") == "plan" else "index"

    connection = get_db()

    if not task_owned_by_current_user(connection, task_id):
        connection.close()
        flash("You don't have access to that task.")
        return redirect(url_for(redirect_to))

    completed_at = datetime.now().strftime("%d %B %Y, %I:%M %p")
    connection.execute(
        "UPDATE tasks SET status='Completed', completed_at=?, time_studied=? WHERE id=?",
        (completed_at, time_studied, task_id)
    )
    connection.commit()
    connection.close()
    flash("Task marked complete." if redirect_to == "index" else "Task marked complete. Rebuild your plan to refresh this list.")
    return redirect(url_for(redirect_to))


@app.route("/start", methods=["POST"])
@login_required
@csrf.exempt
def start():
    task_id = request.json["task_id"]
    started_at = request.json["started_at"]

    connection = get_db()

    if not task_owned_by_current_user(connection, task_id):
        connection.close()
        return jsonify({"error": "forbidden"}), 403

    session_id = insert_and_get_id(
        connection,
        "INSERT INTO study_sessions (task_id, started_at) VALUES (?, ?)",
        (task_id, started_at)
    )
    connection.commit()
    connection.close()
    return jsonify({"session_id": session_id})


@app.route("/stop", methods=["POST"])
@login_required
@csrf.exempt
def stop():
    session_id = request.json["session_id"]
    stopped_at = request.json["stopped_at"]

    connection = get_db()

    if not session_owned_by_current_user(connection, session_id):
        connection.close()
        return jsonify({"error": "forbidden"}), 403

    connection.execute(
        "UPDATE study_sessions SET stopped_at=? WHERE id=?",
        (stopped_at, session_id)
    )
    connection.commit()
    connection.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# V2 — Planning Engine
# ---------------------------------------------------------------------------

PRIORITY_SCORE = {1: 10, 2: 20, 3: 30}


def calculate_urgency_score(deadline_str):
    if not deadline_str:
        return 0
    try:
        deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
    except ValueError:
        return 0

    days_until = (deadline_date - date.today()).days
    if days_until <= 0:
        return 50
    elif days_until == 1:
        return 40
    elif days_until <= 3:
        return 25
    elif days_until <= 7:
        return 15
    return 5


def calculate_task_score(task):
    return PRIORITY_SCORE.get(task["priority"], 10) + calculate_urgency_score(task["deadline"])


def build_plan(tasks, available_minutes):
    """0/1 knapsack maximizing score within a minutes budget.
    Uses each task's 'plan_minutes' (adjusted estimate) as its cost."""
    capacity = max(int(available_minutes), 0)
    candidates = [t for t in tasks if t["plan_minutes"] <= capacity]
    n = len(candidates)

    if n == 0 or capacity == 0:
        return []

    dp = [[0] * (capacity + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost = candidates[i - 1]["plan_minutes"]
        score = candidates[i - 1]["score"]
        for w in range(capacity + 1):
            dp[i][w] = dp[i - 1][w]
            if cost <= w:
                dp[i][w] = max(dp[i][w], dp[i - 1][w - cost] + score)

    chosen = []
    w = capacity
    for i in range(n, 0, -1):
        if dp[i][w] != dp[i - 1][w]:
            chosen.append(candidates[i - 1])
            w -= candidates[i - 1]["plan_minutes"]
    chosen.reverse()
    return chosen


# ---------------------------------------------------------------------------
# V3 — Adaptive learning engine
# ---------------------------------------------------------------------------

def get_pace_multiplier(connection, topic_id, subject_id):
    """
    Looks at the user's completed tasks to see how their actual time compares
    to their estimates, and returns a multiplier to apply to future estimates
    in the same topic (falling back to the subject if the topic has too little
    history). Clamped so one wildly off task can't skew things too hard.
    """
    def ratio_from_rows(rows):
        ratios = []
        for row in rows:
            estimated = row["estimated_minutes"]
            actual_minutes = row["time_studied"] / 1000 / 60
            if estimated and actual_minutes > 0:
                ratios.append(actual_minutes / estimated)
        return ratios

    topic_rows = connection.execute(
        """
        SELECT estimated_minutes, time_studied FROM tasks
        WHERE topic_id = ? AND status = 'Completed' AND time_studied > 0
        """,
        (topic_id,)
    ).fetchall()

    ratios = ratio_from_rows(topic_rows)

    if len(ratios) < 2:
        subject_rows = connection.execute(
            """
            SELECT tasks.estimated_minutes, tasks.time_studied
            FROM tasks
            JOIN topics ON tasks.topic_id = topics.id
            WHERE topics.subject_id = ? AND tasks.status = 'Completed' AND tasks.time_studied > 0
            """,
            (subject_id,)
        ).fetchall()
        ratios = ratio_from_rows(subject_rows)

    if len(ratios) < 2:
        return 1.0

    average_ratio = sum(ratios) / len(ratios)
    return max(0.5, min(average_ratio, 2.5))


@app.route("/plan", methods=["GET", "POST"])
@login_required
def plan():
    plan_tasks = None
    total_minutes = None
    available_minutes = None

    if request.method == "POST":
        available_minutes = int(request.form["available_minutes"])
        connection = get_db()

        tasks = connection.execute(
            """
            SELECT tasks.*, topics.name AS topic_name, subjects.name AS subject_name
            FROM tasks
            JOIN topics ON tasks.topic_id = topics.id
            JOIN subjects ON topics.subject_id = subjects.id
            WHERE tasks.status = 'Incomplete' AND subjects.user_id = ?
            """,
            (current_user.id,)
        ).fetchall()
        tasks = [dict(t) for t in tasks]

        available_tasks = []
        for task in tasks:
            result = connection.execute(
                """
                SELECT
                    COUNT(status) AS total_prerequisites,
                    COUNT(CASE WHEN status = 'Completed' THEN 1 END) AS completed_prerequisites
                FROM prerequisites
                JOIN tasks ON prerequisites.prerequisite_id = tasks.id
                WHERE task_id = ?
                """,
                (task["id"],)
            ).fetchone()

            if result["total_prerequisites"] == result["completed_prerequisites"]:
                subject_id = connection.execute(
                    "SELECT subject_id FROM topics WHERE id = ?",
                    (task["topic_id"],)
                ).fetchone()["subject_id"]
                multiplier = get_pace_multiplier(connection, task["topic_id"], subject_id)
                task["pace_multiplier"] = round(multiplier, 2)
                task["plan_minutes"] = max(1, round(task["estimated_minutes"] * multiplier))
                task["score"] = calculate_task_score(task)
                available_tasks.append(task)

        connection.close()

        plan_tasks = build_plan(available_tasks, available_minutes)
        plan_tasks.sort(key=lambda t: t["score"], reverse=True)
        total_minutes = sum(t["plan_minutes"] for t in plan_tasks)

    return render_template(
        "plan.html",
        plan_tasks=plan_tasks,
        available_minutes=available_minutes,
        total_minutes=total_minutes
    )


# ---------------------------------------------------------------------------
# V4 — Analytics & Intelligence
# ---------------------------------------------------------------------------

def parse_completed_at(value):
    try:
        return datetime.strptime(value, "%d %B %Y, %I:%M %p")
    except (ValueError, TypeError):
        return None


@app.route("/analytics")
@login_required
def analytics():
    connection = get_db()

    tasks = connection.execute(
        """
        SELECT tasks.*, topics.name AS topic_name, subjects.name AS subject_name
        FROM tasks
        JOIN topics ON tasks.topic_id = topics.id
        JOIN subjects ON topics.subject_id = subjects.id
        WHERE subjects.user_id = ?
        """,
        (current_user.id,)
    ).fetchall()
    tasks = [dict(t) for t in tasks]
    connection.close()

    total_tasks = len(tasks)
    completed = [t for t in tasks if t["status"] == "Completed"]
    total_hours = sum(t["time_studied"] for t in tasks) / 1000 / 3600

    by_subject = {}
    for t in tasks:
        s = by_subject.setdefault(t["subject_name"], {
            "total": 0, "completed": 0, "minutes_studied": 0
        })
        s["total"] += 1
        if t["status"] == "Completed":
            s["completed"] += 1
        s["minutes_studied"] += t["time_studied"] / 1000 / 60

    subject_labels = list(by_subject.keys())
    subject_hours = [round(v["minutes_studied"] / 60, 2) for v in by_subject.values()]
    subject_completion = [
        round((v["completed"] / v["total"]) * 100, 1) if v["total"] else 0
        for v in by_subject.values()
    ]

    accuracy_samples = []
    for t in completed:
        if t["estimated_minutes"] and t["time_studied"] > 0:
            actual = t["time_studied"] / 1000 / 60
            accuracy_samples.append(abs(actual - t["estimated_minutes"]) / t["estimated_minutes"])
    avg_estimate_error = (
        round(sum(accuracy_samples) / len(accuracy_samples) * 100, 1)
        if accuracy_samples else None
    )

    weeks = []
    week_counts = []
    today = date.today()
    for i in range(7, -1, -1):
        week_start = today - timedelta(days=today.weekday() + i * 7)
        week_end = week_start + timedelta(days=6)
        weeks.append(week_start.strftime("%d %b"))
        count = 0
        for t in completed:
            completed_dt = parse_completed_at(t["completed_at"])
            if completed_dt and week_start <= completed_dt.date() <= week_end:
                count += 1
        week_counts.append(count)

    completed_breakdown = []
    for t in completed:
        actual_minutes = round(t["time_studied"] / 1000 / 60, 1) if t["time_studied"] else 0
        error_pct = None
        if t["estimated_minutes"] and actual_minutes:
            error_pct = round(abs(actual_minutes - t["estimated_minutes"]) / t["estimated_minutes"] * 100, 1)
        completed_breakdown.append({
            "title": t["title"],
            "subject_name": t["subject_name"],
            "topic_name": t["topic_name"],
            "estimated_minutes": t["estimated_minutes"],
            "actual_minutes": actual_minutes,
            "error_pct": error_pct,
            "completed_at": t["completed_at"],
        })
    completed_breakdown.sort(key=lambda t: t["completed_at"] or "", reverse=True)

    chart_data = {
        "subject_labels": subject_labels,
        "subject_hours": subject_hours,
        "subject_completion": subject_completion,
        "week_labels": weeks,
        "week_counts": week_counts,
    }

    return render_template(
        "analytics.html",
        total_tasks=total_tasks,
        completed_count=len(completed),
        total_hours=round(total_hours, 1),
        avg_estimate_error=avg_estimate_error,
        completed_breakdown=completed_breakdown,
        chart_data=json.dumps(chart_data)
    )


# ---------------------------------------------------------------------------
# V5 — AI learning assistant
# ---------------------------------------------------------------------------

GOAL_TEMPLATES = {
    "algorithms": {
        "topics": [
            ("Complexity & Big-O", ["Learn Big-O notation basics", "Practice analyzing time complexity"]),
            ("Arrays & Strings", ["Two-pointer technique", "Sliding window practice"]),
            ("Trees & Graphs", ["Binary search trees", "DFS and BFS practice"]),
            ("Dynamic Programming", ["Memoization basics", "Classic DP problems"]),
        ]
    },
    "calculus": {
        "topics": [
            ("Limits", ["Limit laws", "Practice limit problems"]),
            ("Derivatives", ["Differentiation rules", "Applications of derivatives"]),
            ("Integrals", ["Basic integration techniques", "Definite integral practice"]),
        ]
    },
}


def generate_breakdown(goal):
    """
    Turns a free-text goal into a topic/task structure. Tries a live model
    call if ANTHROPIC_API_KEY is set (best-effort, optional dependency);
    otherwise falls back to a known template or a generic structure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic  # optional dependency, only used if installed
            client = anthropic.Anthropic(api_key=api_key)
            prompt = (
                f"Break the learning goal '{goal}' into 3-5 topics, each with "
                "2-3 concrete study tasks. Respond ONLY with JSON in the shape "
                '{"topics": [{"name": "...", "tasks": ["...", "..."]}]}. '
                "No other text."
            )
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            data = json.loads(text)
            if "topics" in data:
                return data
        except Exception:
            pass  # fall through to template/generic breakdown

    key = goal.strip().lower()
    if key in GOAL_TEMPLATES:
        return {
            "topics": [
                {"name": name, "tasks": tasks}
                for name, tasks in GOAL_TEMPLATES[key]["topics"]
            ]
        }

    title = goal.strip().title()
    return {
        "topics": [
            {"name": f"{title} Foundations", "tasks": [
                f"Learn the core vocabulary of {title}",
                f"Read/watch an intro overview of {title}"
            ]},
            {"name": f"{title} Core Practice", "tasks": [
                f"Work through beginner {title} exercises",
                f"Apply {title} to a small practice project"
            ]},
            {"name": f"{title} Advanced Application", "tasks": [
                f"Tackle an intermediate {title} problem set",
                f"Get feedback on your {title} work"
            ]},
            {"name": f"{title} Review & Assessment", "tasks": [
                f"Review weak spots in {title}",
                f"Take a self-assessment or mock test in {title}"
            ]},
        ]
    }


_PLAN_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _extract_plan_deadline(heading_text):
    """
    Looks for a date range ending in "... -> Month YYYY" (also accepts '→' or
    the word 'to' as the separator) and returns an ISO date for the last day
    of that end month, or None if no such range is found. Only the end month
    is required — "Feb -> Mar 2027" (no year on the first month) works too.
    """
    m = re.search(
        r'(?:→|->|\bto\b)\s*([A-Za-z]+)\.?\s+(\d{4})',
        heading_text,
        re.IGNORECASE
    )
    if not m:
        return None
    end_month_name, end_year = m.group(1).lower(), int(m.group(2))
    month = _PLAN_MONTHS.get(end_month_name)
    if not month:
        return None
    if month == 12:
        last_day = 31
    else:
        last_day = (date(end_year, month + 1, 1) - timedelta(days=1)).day
    return date(end_year, month, last_day).isoformat()


def parse_plan_markdown(text):
    """
    Parses a loosely-structured markdown study plan into subjects/topics/tasks:
      - A line starting with '#' or '##' starts a new SUBJECT. If its heading
        text contains a "Month YYYY -> Month YYYY" style range, the end month
        becomes the deadline for every task under it.
      - A line starting with '###' or more starts a new TOPIC under the
        current subject (a 'General' topic is created if none exists yet).
      - A line starting with '- [ ]' or '* [ ]' becomes a TASK under the
        current topic.
      - Anything else (prose, blank lines) is ignored.
    Subjects/topics that end up with no tasks at all are dropped.
    """
    subjects = []
    current_subject = None
    current_topic = None

    def ensure_subject():
        nonlocal current_subject
        if current_subject is None:
            current_subject = {"name": "Imported plan", "deadline": None, "topics": []}
            subjects.append(current_subject)
        return current_subject

    def ensure_topic():
        nonlocal current_topic
        subj = ensure_subject()
        if current_topic is None:
            current_topic = {"name": "General", "tasks": []}
            subj["topics"].append(current_topic)
        return current_topic

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading_match:
            hashes, title = heading_match.groups()
            title = re.sub(r'\*\*(.*?)\*\*', r'\1', title).strip()
            title = re.sub(r'^\d+\.\s*', '', title)
            if len(hashes) <= 2:
                current_subject = {
                    "name": title,
                    "deadline": _extract_plan_deadline(title),
                    "topics": []
                }
                subjects.append(current_subject)
                current_topic = None
            else:
                ensure_subject()
                current_topic = {"name": title, "tasks": []}
                current_subject["topics"].append(current_topic)
            continue

        task_match = re.match(r'^[-*]\s+\[\s?[xX ]?\s?\]\s+(.*)', line)
        if task_match:
            raw = re.sub(r'\*\*(.*?)\*\*', r'\1', task_match.group(1)).strip()
            # Optional "Title | ..." shorthand. Everything after the title,
            # in any order: a bare number is treated as minutes, anything
            # starting with http(s):// is the resource link, anything else
            # is the description.
            parts = [p.strip() for p in raw.split('|')]
            task_title = parts[0]
            task_minutes = None
            task_url = None
            task_description = None
            for extra in parts[1:]:
                if not extra:
                    continue
                if re.fullmatch(r'\d+(\.\d+)?', extra):
                    task_minutes = int(round(float(extra)))
                elif re.match(r'^https?://', extra, re.IGNORECASE):
                    task_url = extra
                elif task_description is None:
                    task_description = extra
                else:
                    task_description = f"{task_description} {extra}"
            if task_title:
                ensure_topic()["tasks"].append({
                    "title": task_title,
                    "resource_url": task_url,
                    "description": task_description,
                    "estimated_minutes": task_minutes,
                })
            continue
        # anything else (prose lines) is ignored

    cleaned = []
    for subj in subjects:
        topics = [t for t in subj["topics"] if t["tasks"]]
        if topics:
            subj["topics"] = topics
            cleaned.append(subj)
    return cleaned


@app.route("/ai_assistant", methods=["GET", "POST"])
@login_required
def ai_assistant():
    breakdown = None
    goal = None

    if request.method == "POST":
        action = request.form.get("action", "generate")

        if action == "generate":
            goal = request.form["goal"].strip()
            breakdown = generate_breakdown(goal)

        elif action == "import":
            goal = request.form["goal"]
            breakdown = json.loads(request.form["breakdown_json"])
            deadline_gap_days = 7

            connection = get_db()
            subject_id = insert_and_get_id(
                connection,
                "INSERT INTO subjects (name, user_id) VALUES (?, ?)",
                (goal, current_user.id)
            )

            for i, topic in enumerate(breakdown["topics"]):
                topic_id = insert_and_get_id(
                    connection,
                    "INSERT INTO topics (name, subject_id) VALUES (?, ?)",
                    (topic["name"], subject_id)
                )
                deadline = (date.today() + timedelta(days=deadline_gap_days * (i + 1))).isoformat()

                for task_title in topic["tasks"]:
                    connection.execute(
                        """
                        INSERT INTO tasks
                            (title, estimated_minutes, priority, difficulty, deadline, topic_id)
                        VALUES (?, 30, 2, 'Medium', ?, ?)
                        """,
                        (task_title, deadline, topic_id)
                    )

            connection.commit()
            connection.close()
            flash(f"Imported \"{goal}\" into your planner.")
            return redirect(url_for("index"))

        elif action == "import_plan":
            plan_text = request.form.get("plan_text", "")
            parsed_subjects = parse_plan_markdown(plan_text)

            if not parsed_subjects:
                flash("Couldn't find any checklist items ('- [ ]' / '* [ ]') in that text — nothing was imported.")
                return render_template("ai_assistant.html", breakdown=None, goal=None)

            connection = get_db()
            task_count = 0
            for subj in parsed_subjects:
                subject_id = insert_and_get_id(
                    connection,
                    "INSERT INTO subjects (name, user_id) VALUES (?, ?)",
                    (subj["name"], current_user.id)
                )

                for topic in subj["topics"]:
                    topic_id = insert_and_get_id(
                        connection,
                        "INSERT INTO topics (name, subject_id) VALUES (?, ?)",
                        (topic["name"], subject_id)
                    )

                    for task in topic["tasks"]:
                        connection.execute(
                            """
                            INSERT INTO tasks
                                (title, description, estimated_minutes, priority, difficulty,
                                 deadline, resource_url, topic_id)
                            VALUES (?, ?, ?, 2, 'Medium', ?, ?, ?)
                            """,
                            (task["title"], task["description"],
                             task["estimated_minutes"] or 30, subj["deadline"],
                             task["resource_url"], topic_id)
                        )
                        task_count += 1

            connection.commit()
            connection.close()
            flash(f"Imported {len(parsed_subjects)} subject(s) and {task_count} task(s) into your planner.")
            return redirect(url_for("index"))

    return render_template("ai_assistant.html", breakdown=breakdown, goal=goal)


# Runs under `python3 app.py` (dev) AND under gunicorn/production, since
# gunicorn imports this module directly and never hits the __main__ guard.
init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
