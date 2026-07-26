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
