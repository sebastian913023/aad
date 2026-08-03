"""Error types.

`NotConfiguredError` and `NoGroundingError` exist so an unconfigured provider or an
empty retrieval surfaces as an explicit gap. They are returned to the model as tool
errors, which is what stops it from filling the hole with a plausible number.
"""

from __future__ import annotations


class AadError(Exception):
    """Base class for all application errors."""


class NotConfiguredError(AadError):
    """A required provider credential or endpoint is missing."""

    def __init__(self, provider: str, hint: str = "") -> None:
        msg = f"provider '{provider}' is not configured"
        if hint:
            msg = f"{msg}: {hint}"
        super().__init__(msg)
        self.provider = provider


class NoGroundingError(AadError):
    """Retrieval returned nothing usable for the requested vehicle."""

    def __init__(self, query: str, vehicle_label: str) -> None:
        super().__init__(
            f"no indexed source material matches {query!r} for {vehicle_label}; "
            "answer unavailable rather than estimated"
        )


class UnscopedRequestError(AadError):
    """A request arrived without enough vehicle identity to scope retrieval."""

    def __init__(self) -> None:
        super().__init__(
            "vehicle must include a VIN or year/make/model before service data can be retrieved"
        )


class ProviderError(AadError):
    """An upstream data provider failed or returned an unusable response."""
