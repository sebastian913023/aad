"""Agent loop tests against a scripted model client — no network, no API key."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aad.agent.agent import DiagnosticAgent
from aad.agent.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_details: Any = None


class ScriptedClient:
    """Replays a fixed list of responses and records the requests it received."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]


def test_tool_schemas_are_wellformed():
    names = {tool["name"] for tool in TOOL_SCHEMAS}
    assert {"decode_vin", "lookup_torque_spec", "lookup_labor_time", "build_estimate"} <= names
    for tool in TOOL_SCHEMAS:
        assert tool["description"].strip()
        assert tool["input_schema"]["type"] == "object"


def test_dispatch_returns_error_payload_for_unknown_tool(indexed_retriever, settings):
    ctx = ToolContext(retriever=indexed_retriever, settings=settings)
    payload, is_error = dispatch_tool("does_not_exist", {}, ctx)
    assert is_error and "unknown tool" in json.loads(payload)["error"]


def test_dispatch_surfaces_domain_errors_with_instruction(indexed_retriever, settings, g35):
    ctx = ToolContext(retriever=indexed_retriever, settings=settings, vehicle=g35)
    payload, is_error = dispatch_tool("search_parts", {"query": "sensor"}, ctx)
    parsed = json.loads(payload)
    assert is_error
    assert parsed["error_type"] == "NotConfiguredError"
    assert "do not substitute" in parsed["instruction"].lower()


def test_dispatch_unscoped_request_is_rejected(indexed_retriever, settings):
    ctx = ToolContext(retriever=indexed_retriever, settings=settings)
    payload, is_error = dispatch_tool("lookup_torque_spec", {"component": "head bolts"}, ctx)
    assert is_error
    assert json.loads(payload)["error_type"] == "UnscopedRequestError"


def test_agent_runs_tool_then_answers(indexed_retriever, settings, g35):
    client = ScriptedClient(
        [
            FakeResponse(
                content=[
                    FakeBlock(
                        type="tool_use",
                        name="lookup_torque_spec",
                        id="toolu_1",
                        input={"component": "camshaft position sensor retaining bolt"},
                    )
                ],
                stop_reason="tool_use",
            ),
            FakeResponse(content=[FakeBlock(type="text", text="9 Nm, M6 x 1.0.")]),
        ]
    )
    agent = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings)
    result = agent.run("What is the CMP sensor bolt torque?", vehicle=g35)

    assert result.answer == "9 Nm, M6 x 1.0."
    assert [call.name for call in result.tool_calls] == ["lookup_torque_spec"]
    assert result.citations, "citations should be harvested from tool results"
    assert result.citations[0]["source"] == "sample_service_manual.md"


def test_agent_passes_tools_and_cached_system_prompt(indexed_retriever, settings, g35):
    client = ScriptedClient([FakeResponse(content=[FakeBlock(type="text", text="ok")])])
    DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings).run(
        "hello", vehicle=g35
    )

    request = client.requests[0]
    assert request["tools"] == TOOL_SCHEMAS
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == settings.model_effort
    # The known vehicle is stated in the user turn, not baked into the cached system prompt.
    assert "G35" in request["messages"][0]["content"]


def test_agent_handles_refusal_without_reading_content(indexed_retriever, settings, g35):
    client = ScriptedClient([FakeResponse(content=[], stop_reason="refusal")])
    result = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings).run(
        "something declined", vehicle=g35
    )
    assert "declined" in result.answer
    assert result.stop_reason == "refusal"


def test_agent_stops_after_turn_budget(indexed_retriever, settings, g35):
    looping = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", name="lookup_torque_spec", id="t", input={"component": "bolt"}
            )
        ],
        stop_reason="tool_use",
    )
    client = ScriptedClient([looping])
    result = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings).run(
        "loop forever", vehicle=g35, max_turns=3
    )
    assert "Stopped after 3 tool-calling turns" in result.answer
    assert len(result.tool_calls) == 3


def test_decode_vin_tool_updates_session_vehicle(indexed_retriever, settings, monkeypatch):
    from aad.providers import vin as vin_provider

    monkeypatch.setattr(
        vin_provider,
        "decode_vin",
        lambda vin, **_: {
            "vehicle": {"vin": vin, "year": 2004, "make": "INFINITI", "model": "G35"},
            "check_digit_valid": True,
        },
    )
    from aad.agent import tools as tools_module

    monkeypatch.setattr(tools_module.vin_provider, "decode_vin", vin_provider.decode_vin)

    ctx = ToolContext(retriever=indexed_retriever, settings=settings)
    dispatch_tool("decode_vin", {"vin": "JNKCV51E04M100000"}, ctx)

    assert ctx.vehicle.make == "INFINITI"
    # A later tool call inherits the decoded vehicle without restating it.
    assert ctx.resolve(None).model == "G35"


def test_agent_withholds_an_answer_the_monitor_blocks(indexed_retriever, settings, g35):
    """A torque value the retrieved documents do not contain must not reach a technician."""
    client = ScriptedClient(
        [
            FakeResponse(
                content=[
                    FakeBlock(
                        type="tool_use",
                        name="lookup_torque_spec",
                        id="toolu_1",
                        input={"component": "camshaft position sensor retaining bolt"},
                    )
                ],
                stop_reason="tool_use",
            ),
            FakeResponse(
                content=[FakeBlock(type="text", text="Torque the CMP sensor bolt to 47 Nm.")]
            ),
        ]
    )
    result = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings).run(
        "What is the CMP sensor bolt torque?", vehicle=g35
    )

    assert "Answer withheld" in result.answer
    assert "47" in result.answer  # the withheld value is named, not hidden
    assert result.unverified_answer == "Torque the CMP sensor bolt to 47 Nm."
    assert result.monitor["verdict"] == "blocked"
    assert result.monitor["task_type"] == "torque_spec"
    assert result.monitor_event_id


def test_agent_records_a_grounded_answer_without_altering_it(indexed_retriever, settings, g35):
    client = ScriptedClient(
        [
            FakeResponse(
                content=[
                    FakeBlock(
                        type="tool_use",
                        name="lookup_torque_spec",
                        id="toolu_1",
                        input={"component": "camshaft position sensor retaining bolt"},
                    )
                ],
                stop_reason="tool_use",
            ),
            FakeResponse(content=[FakeBlock(type="text", text="Torque it to 9 Nm.")]),
        ]
    )
    result = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=settings).run(
        "What is the CMP sensor bolt torque?", vehicle=g35
    )
    assert result.answer == "Torque it to 9 Nm."
    assert result.monitor["verdict"] == "grounded"
    assert result.unverified_answer is None


def test_monitoring_can_be_switched_off(indexed_retriever, settings, g35):
    off = settings.model_copy(update={"monitor_enabled": False})
    client = ScriptedClient(
        [FakeResponse(content=[FakeBlock(type="text", text="Torque it to 47 Nm.")])]
    )
    result = DiagnosticAgent(client=client, retriever=indexed_retriever, settings=off).run(
        "torque?", vehicle=g35
    )
    assert result.answer == "Torque it to 47 Nm."
    assert result.monitor is None
