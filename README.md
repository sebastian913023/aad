# aad — Task Tracker

A minimal, dependency-free full-stack task tracker: SQLite-backed REST API
(Python stdlib only) with a single-page vanilla JS/HTML frontend.

## Structure

```
backend/
  auth.py     JWT-style token auth (HMAC-SHA256) + PBKDF2 password hashing
  db.py       SQLite persistence layer (users, tasks)
  server.py   REST API (http.server) + static frontend serving
frontend/
  index.html  Login/register + task list UI, calls the REST API
tests/        unittest suite covering auth, db, and the HTTP API
data/         SQLite database file (created on first run, gitignored)
```

## Running

Requires Python 3.10+, no third-party packages.

```bash
python3 backend/server.py
```

Then open http://127.0.0.1:8765/ in a browser.

By default the server binds to `127.0.0.1:8765`. Set `CLAWTEAM_SECRET_KEY`
in the environment to override the token-signing secret (recommended for
anything beyond local use).

## API

| Method | Path              | Auth | Body                          |
|--------|-------------------|------|--------------------------------|
| POST   | /api/register     | no   | `{username, password}`        |
| POST   | /api/login        | no   | `{username, password}` → token |
| GET    | /api/tasks        | yes  | —                              |
| POST   | /api/tasks        | yes  | `{title}`                      |
| PATCH  | /api/tasks/{id}   | yes  | `{title?, done?}`              |
| DELETE | /api/tasks/{id}   | yes  | —                              |
| GET    | /healthz          | no   | —                              |

Authenticated requests send `Authorization: Bearer <token>`.

## Tests

The suite is stdlib `unittest`, so it needs no install step:

```bash
python3 -m unittest discover -s tests -t .
```

It covers token signing and forgery rejection, password hashing, the SQLite
layer including per-user scoping, and the HTTP API end to end (a real server
is booted on an ephemeral port against a temporary database).

## CI

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

- **test** — the suite across Python 3.10 / 3.11 / 3.12 / 3.13
- **lint** — `ruff check` and `ruff format --diff`
- **smoke** — boots the real server and exercises the API over HTTP

Lint configuration lives in `pyproject.toml`. To run it locally:

```bash
pip install ruff==0.15.8
ruff check .
ruff format --diff .
```
