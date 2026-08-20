"""Vercel Python entrypoint.

Vercel's Python runtime looks for an ASGI/WSGI `app` object in a file under
`api/`. This project uses a `src/` layout (see the `[tool.hatch.build]`
table in `pyproject.toml`), which isn't on `sys.path` by default in the
serverless build, so it's added here before importing the real application
object built in `aad.api`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aad.api import app  # noqa: E402  (import must follow the sys.path fix-up)

__all__ = ["app"]
