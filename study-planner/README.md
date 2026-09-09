# Study Planner — V1 to V6

A self-hosted study/task planner that grew from a simple task tracker into:

- **V1 — Core system**: subjects → topics → tasks, prerequisites, live timers.
- **V2 — Planning Engine** (`/plan`): pick a time budget, get the best-fit set
  of unlocked tasks ranked by priority and deadline urgency.
- **V3 — Adaptive learning**: your planner learns from how long tasks *actually*
  took you and adjusts future time estimates per topic/subject.
- **V4 — Analytics** (`/analytics`): time studied per subject, completion rates,
  estimate accuracy, and a weekly completed-tasks trend, charted with Chart.js.
- **V5 — AI Assistant** (`/ai_assistant`): type a goal, get a topic/task
  breakdown you can import straight into your planner. Uses a template-based
  breakdown by default; if you set `ANTHROPIC_API_KEY`, it'll try a live model
  call for a richer breakdown first.
- **V6 — Production**: user accounts, hashed passwords, per-user data isolation,
  CSRF protection, and a test suite.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

The database is created automatically on first run (`database.db`, SQLite).

## Running locally

```bash
export SECRET_KEY="something-random-and-long"   # Windows: set SECRET_KEY=...
python3 app.py
```

Visit `http://localhost:5000`, register an account, and start adding subjects.

### Optional: smarter AI breakdowns

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
```

Without this, the AI Assistant still works — it falls back to a built-in
template-based breakdown.

## Running the tests

```bash
pip install -r requirements.txt
pytest
```

## Deploying (Render — free tier, no server management)

1. Push this project to a GitHub repository (public or private both work here,
   unlike GitHub Pages).
2. Go to [render.com](https://render.com) and sign up (you can use your GitHub
   account to sign in directly).
3. **New** → **Web Service** → connect the GitHub repo.
4. Render should auto-detect Python. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app` (already declared in the `Procfile`,
     Render usually picks it up automatically)
5. Under **Environment**, add an environment variable:
   - `SECRET_KEY` = a long random string (don't use the `dev-secret-change-me`
     default)
   - Optionally `ANTHROPIC_API_KEY` if you want live AI breakdowns
6. Deploy. Render gives you a `https://<your-service>.onrender.com` URL —
   that's your live site.

### Notes for this hosting setup

- **Database persistence**: Render's free web services use an ephemeral
  filesystem — `database.db` will reset on redeploys/restarts. For anything
  beyond trying it out, add a Render **Disk** (persistent volume) mounted at
  the project directory, or move to Postgres. This is the one thing worth
  fixing before relying on this daily.
- Free-tier Render services spin down after inactivity and take ~30-60
  seconds to wake back up on the next visit — normal, not a bug.
- SQLite is fine for single-user or small-scale use; if you ever have many
  concurrent users, move to Postgres.

### Alternative: any Flask host

This is a normal Flask app, so any standard Flask host works — Railway,
Fly.io, PythonAnywhere, or your own VM with gunicorn behind nginx/Caddy:

```bash
export SECRET_KEY="something-random-and-long"
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

## Notes on the V6 schema change

V6 added a `users` table and ownership (`user_id`) on subjects. If you have an
existing `database.db` from an earlier version, it won't have these — the
simplest path is to start fresh (delete the old `database.db` and let the app
recreate it) rather than trying to migrate old data in place.
