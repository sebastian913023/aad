"""Model client construction.

Two deployment targets. The first-party Claude API is the default; Amazon Bedrock is
supported for VPC-resident deployments, which is what the compliance requirements
(TISAX, SOC 2) push toward. Bedrock model ids carry an `anthropic.` prefix and the
Bedrock path does not support server-side refusal fallbacks — both handled here so the
rest of the codebase does not branch on provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError


@dataclass(slots=True)
class ModelClient:
    client: Any
    model: str
    supports_server_fallbacks: bool

    def create(self, **kwargs: Any) -> Any:
        """Issue a Messages request, adding refusal fallbacks where supported.

        On the Claude API, a safety-classifier decline is re-served by Anthropic's
        recommended fallback model inside the same call rather than surfacing as a dead
        end. Automotive work brushes against security-adjacent topics (immobilizers,
        key programming, module reflashing) often enough that this matters.
        """
        if self.supports_server_fallbacks:
            return self.client.beta.messages.create(
                model=self.model,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            )
        return self.client.messages.create(model=self.model, **kwargs)


def build_client(settings: Settings | None = None) -> ModelClient:
    settings = settings or get_settings()

    if settings.provider == "bedrock":
        from anthropic import AnthropicBedrockMantle

        model = settings.model
        if not model.startswith("anthropic."):
            model = f"anthropic.{model}"
        return ModelClient(
            client=AnthropicBedrockMantle(aws_region=settings.aws_region),
            model=model,
            supports_server_fallbacks=False,
        )

    if not settings.anthropic_api_key:
        # Every other provider in this system fails this way on a missing credential;
        # the model call is not special. Without this check the Anthropic SDK raises
        # a bare TypeError deep inside request-building, which reaches a caller as an
        # unhandled 500 instead of the 501 "not configured" every other gap produces.
        raise NotConfiguredError("anthropic", "set ANTHROPIC_API_KEY")

    from anthropic import Anthropic

    return ModelClient(
        client=Anthropic(api_key=settings.anthropic_api_key),
        model=settings.model,
        supports_server_fallbacks=True,
    )
