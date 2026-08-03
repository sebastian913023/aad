"""Declarative provider profiles.

Mitchell 1, ALLDATA, NAPA and the OBD2 scan services are partner-gated: there is no
public API contract to code against, and each one differs in auth scheme, path, query
parameter names and response envelope. Hard-coding one guessed REST shape per provider
meant every vendor integration was a Python patch.

A profile moves all of that into data. Pointing the system at a real subscription is
then: set two environment variables, and adjust a JSON file to match the vendor's own
API documentation. No code change, and the mapping is inspectable and testable.

`verified: false` on a shipped profile means exactly what it says — the shape is a
placeholder derived from the vendor's product documentation, not from a contract anyone
has exercised. `aad providers test` is how it becomes true.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from aad.errors import ProviderError

BUILTIN_PROFILE_DIR = Path(__file__).resolve().parent / "profiles"

# Canonical provider slugs. One per commercial integration point.
ProviderName = Literal["torque", "labor", "parts", "wiring", "obd2"]
PROVIDER_NAMES: tuple[str, ...] = ("torque", "labor", "parts", "wiring", "obd2")


class AuthConfig(BaseModel):
    """How the credential is attached to the request."""

    type: Literal["bearer", "header", "query", "basic", "none"] = "bearer"
    # Header name for `header` auth (e.g. "X-Api-Key"), or query parameter name
    # for `query` auth (e.g. "apikey").
    name: str | None = None
    # For `basic`: the env var holding the username. The credential env var is
    # then treated as the password.
    username_env: str | None = None


class ProviderProfile(BaseModel):
    """A complete description of one vendor endpoint."""

    name: str
    vendor: str
    description: str = ""
    docs_url: str | None = None
    # False until someone has run `aad providers test` against the real service
    # and confirmed the mapping. Surfaced everywhere the profile is reported.
    verified: bool = False

    base_url_env: str
    credential_env: str
    auth: AuthConfig = Field(default_factory=AuthConfig)

    method: Literal["GET", "POST"] = "GET"
    path: str = "/"
    # request parameter name -> source key (year, make, model, engine, vin, plus
    # the endpoint's own argument such as `component` or `operation`)
    params: dict[str, str] = Field(default_factory=dict)
    static_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    # Dotted path to the list of results in the response body. Empty means the
    # body is itself the list.
    results_path: str = ""
    # our field name -> dotted path within each result item
    field_map: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0

    # --- configuration state ---------------------------------------------
    def base_url(self, env: dict[str, str] | None = None) -> str | None:
        env = os.environ if env is None else env
        value = (env.get(self.base_url_env) or "").strip()
        return value or None

    def credential(self, env: dict[str, str] | None = None) -> str | None:
        env = os.environ if env is None else env
        value = (env.get(self.credential_env) or "").strip()
        return value or None

    def is_configured(self, env: dict[str, str] | None = None) -> bool:
        if self.base_url(env) is None:
            return False
        if self.auth.type == "none":
            return True
        return self.credential(env) is not None

    def missing(self, env: dict[str, str] | None = None) -> list[str]:
        gaps: list[str] = []
        if self.base_url(env) is None:
            gaps.append(self.base_url_env)
        if self.auth.type != "none" and self.credential(env) is None:
            gaps.append(self.credential_env)
        if self.auth.type == "basic" and self.auth.username_env:
            env_map = os.environ if env is None else env
            if not (env_map.get(self.auth.username_env) or "").strip():
                gaps.append(self.auth.username_env)
        return gaps

    def validate_shape(self) -> list[str]:
        """Static problems that would make a live call meaningless."""
        problems: list[str] = []
        if self.auth.type in {"header", "query"} and not self.auth.name:
            problems.append(f"auth.type is {self.auth.type!r} but auth.name is not set")
        if self.auth.type == "basic" and not self.auth.username_env:
            problems.append("auth.type is 'basic' but auth.username_env is not set")
        if not self.path.startswith("/"):
            problems.append(f"path {self.path!r} must start with '/'")
        if not self.field_map:
            problems.append("field_map is empty — results would be returned unmapped")
        return problems


def _load_dir(directory: Path) -> dict[str, ProviderProfile]:
    profiles: dict[str, ProviderProfile] = {}
    if not directory.is_dir():
        return profiles
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider profile {path} is not valid JSON: {exc}") from exc
        profile = ProviderProfile.model_validate(data)
        profiles[profile.name] = profile
    return profiles


def load_profiles(override_dir: Path | str | None = None) -> dict[str, ProviderProfile]:
    """Built-in profiles, with any same-named profile in `override_dir` winning.

    Overrides are whole-profile replacements rather than deep merges: a partially
    merged endpoint description is harder to reason about than an explicit one,
    and these are short files.
    """
    profiles = _load_dir(BUILTIN_PROFILE_DIR)
    if override_dir:
        profiles.update(_load_dir(Path(override_dir)))
    return profiles


def get_profile(name: str, override_dir: Path | str | None = None) -> ProviderProfile:
    profiles = load_profiles(override_dir)
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise ProviderError(f"no provider profile named {name!r} (known: {known})")
    return profiles[name]
