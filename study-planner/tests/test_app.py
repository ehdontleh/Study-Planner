import importlib
import os
import sys
import tempfile

import pytest

# Point the app at a fresh temp DB *before* importing it, since app.py reads
# STUDY_DB_PATH and calls init_db() at import time.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def app_module():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    os.remove(db_path)  # init_db() should create the schema from scratch
    os.environ["STUDY_DB_PATH"] = db_path
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ.pop("ANTHROPIC_API_KEY", None)  # force template fallback in tests
    os.environ.pop("DATABASE_URL", None)  # force the SQLite path for tests

    import app as app_module
    importlib.reload(app_module)  # re-run module top-level with the new DB path
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    yield app_module

    os.remove(db_path)


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()


def register(client, username="alice", password="password123"):
    return client.post("/register", data={
        "username": username, "password": password, "confirm_password": password
    }, follow_redirects=True)


def login(client, username="alice", password="password123"):
    return client.post("/login", data={
        "username": username, "password": password
    }, follow_redirects=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_register_and_login(client):
    resp = register(client)
    assert resp.status_code == 200

    resp = login(client)
    assert resp.status_code == 200
    assert b"Your planner" in resp.data


def test_cannot_register_duplicate_username(client):
    register(client)
    resp = register(client)
    assert b"already taken" in resp.data


def test_login_required_redirects_to_login(client):
    resp = client.get("/", follow_redirects=True)
    assert b"Log in" in resp.data or b"log in" in resp.data.lower()


# ---------------------------------------------------------------------------
# Per-user data isolation
# ---------------------------------------------------------------------------

def test_users_cannot_see_each_others_subjects(client, app_module):
    register(client, "alice", "password123")
    login(client, "alice", "password123")
    client.post("/add_subject", data={"name": "Alice's Subject"}, follow_redirects=True)
    client.get("/logout")

    register(client, "bob", "password123")
    login(client, "bob", "password123")
    resp = client.get("/", follow_redirects=True)

    assert b"Alice's Subject" not in resp.data


# ---------------------------------------------------------------------------
# Planning engine
# ---------------------------------------------------------------------------

def test_plan_builds_within_budget(client):
    register(client)
    login(client)

    client.post("/add_subject", data={"name": "Test Subject"}, follow_redirects=True)
    connection = None

    import app as app_module
    conn = app_module.get_db()
    subject_id = conn.execute("SELECT id FROM subjects LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO topics (name, subject_id) VALUES (?, ?)", ("Topic A", subject_id))
    conn.commit()
    topic_id = conn.execute("SELECT id FROM topics LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO tasks (title, estimated_minutes, priority, topic_id) VALUES (?, ?, ?, ?)",
        ("Quick task", 20, 3, topic_id)
    )
    conn.commit()
    conn.close()

    resp = client.post("/plan", data={"available_minutes": "30"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Quick task" in resp.data


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def test_analytics_page_loads_with_no_data(client):
    register(client)
    login(client)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert b"How you" in resp.data


# ---------------------------------------------------------------------------
# AI assistant (template fallback, no API key in test env)
# ---------------------------------------------------------------------------

def test_ai_breakdown_uses_template_for_known_goal(client):
    register(client)
    login(client)
    resp = client.post("/ai_assistant", data={"action": "generate", "goal": "algorithms"})
    assert resp.status_code == 200
    assert b"Complexity" in resp.data


def test_ai_breakdown_generic_for_unknown_goal(client):
    register(client)
    login(client)
    resp = client.post("/ai_assistant", data={"action": "generate", "goal": "underwater basket weaving"})
    assert resp.status_code == 200
    assert b"Foundations" in resp.data
