"""Vercel Python entrypoint.

Vercel's Python runtime looks for an ASGI/WSGI `app` object in a file under `api/`. This
project uses a `src/` layout (see the `[tool.hatch.build]` table in `pyproject.toml`),
which isn't on `sys.path` by default in the serverless build, so it's added here before
importing the real application object built in `aad.api`.

If the real app cannot be imported, a stub app is served instead that reports *what*
failed (exception type and message only, never a traceback or paths). A bare
FUNCTION_INVOCATION_FAILED from the platform says nothing about the cause; this does.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from aad.api import app
except Exception as exc:  # noqa: BLE001 - deliberately broad: report any startup failure
    _failure = {
        "status": "startup_failed",
        "error": type(exc).__name__,
        "message": str(exc)[:500],
        "src_dir_exists": _SRC.is_dir(),
    }

    async def app(scope, receive, send):  # type: ignore[no-redef]
        if scope["type"] != "http":
            return
        import json

        body = json.dumps(_failure).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"access-control-allow-origin", b"*"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["app"]
