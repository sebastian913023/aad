"""Tests for the grounding monitor.

The load-bearing case is `test_fabricated_torque_value_is_blocked`: a plausible,
well-cited, confidently-worded torque value that is simply not in the source. If that
ever passes as grounded, the rest of this system's safety story is fiction.
"""

from __future__ import annotations

import pytest

from aad.config import Settings
from aad.ingest.embeddings import LocalHashEmbedder
from aad.monitor.claims import extract_claims
from aad.monitor.harvest import infer_task_type, sources_from_tool_results
from aad.monitor.judge import JudgeVerdict
from aad.monitor.lsc import citation_validity, lexical_grounding, value_appears_in
from aad.monitor.pipeline import monitor_output
from aad.monitor.store import MonitorStore

SOURCE = """
CAMSHAFT POSITION SENSOR — REMOVAL AND INSTALLATION

Remove the sensor retaining bolt. On installation, tighten the camshaft position
sensor bolt to 9 N·m (80 in-lb). Do not exceed this value; the sensor housing is
plastic and will crack.

CYLINDER HEAD BOLTS
Tighten in sequence to 40 Nm, then angle-tighten a further 90 degrees.
"""


@pytest.fixture
def embedder() -> LocalHashEmbedder:
    return LocalHashEmbedder(dim=512)


@pytest.fixture
def monitor_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        index_dir=tmp_path / "index",
        cache_db=tmp_path / "cache.sqlite3",
        monitor_db=tmp_path / "monitor.sqlite3",
        embedding_backend="local",
        vector_backend="local",
        embedding_dim=512,
    )


def _monitor(output: str, settings: Settings, embedder, **kwargs):
    return monitor_output(
        question=kwargs.pop("question", "What is the camshaft position sensor bolt torque?"),
        output=output,
        sources=kwargs.pop("sources", {"manual#c1": SOURCE}),
        task_type=kwargs.pop("task_type", "torque_spec"),
        settings=settings,
        embedder=embedder,
        **kwargs,
    )


# --- claim extraction -----------------------------------------------------


def test_extracts_torque_labor_and_wire_claims():
    claims = extract_claims(
        "Tighten the bolt to 9 N·m. Labor time is 0.7 hrs. The signal wire is green."
    )
    kinds = {c.claim_type for c in claims}
    assert kinds == {"torque", "labor_hours", "wire_color"}
    assert {c.value for c in claims if c.claim_type == "torque"} == {"9"}


def test_extracts_both_ends_of_a_torque_range():
    claims = extract_claims("Torque to 25-30 Nm.")
    assert {c.value for c in claims if c.claim_type == "torque"} == {"25", "30"}


def test_prose_without_specifications_yields_no_claims():
    claims = extract_claims(
        "The camshaft position sensor reports rotational position to the ECM. "
        "A failure typically sets a P0340 and may cause a long crank."
    )
    assert claims == []


# --- LSC primitives -------------------------------------------------------


def test_value_match_respects_digit_boundaries():
    assert value_appears_in("9", "nm", "tighten to 9 N·m")
    # 9 must not match inside 19.
    assert not value_appears_in("9", "nm", "tighten to 19 Nm")


def test_unit_must_sit_next_to_the_number():
    # The number and the unit both appear, but not together.
    assert not value_appears_in("9", "nm", "there are 9 bolts, each torqued in Nm units")


def test_unit_spelling_variants_still_ground():
    assert lexical_grounding("9", "nm", ["tighten to 9 N·m"]) == 1.0
    assert lexical_grounding("80", "in-lb", ["9 N·m (80 in-lb)"]) == 1.0


def test_converted_values_do_not_count_as_grounded():
    # 9 Nm is ~80 in-lb, but a converted number is not a published number.
    assert lexical_grounding("6.6", "lb-ft", [SOURCE]) == 0.0


def test_citation_validity_penalises_invented_sources():
    assert citation_validity(["a", "b"], {"a"}) == 0.5
    assert citation_validity([], {"a"}) == 0.0


# --- the pipeline ---------------------------------------------------------


def test_grounded_output_passes(monitor_settings, embedder):
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m. [manual#c1]",
        monitor_settings,
        embedder,
    )
    assert result.verdict == "grounded"
    assert result.faithfulness == 1.0
    assert result.citation_accuracy == 1.0
    assert not result.needs_human_review


def test_fabricated_torque_value_is_blocked(monitor_settings, embedder):
    """The whole point. A confident, cited, plausible value that is not in the source."""
    result = _monitor(
        "Per the service manual, tighten the camshaft position sensor bolt to 12 N·m. "
        "[manual#c1]",
        monitor_settings,
        embedder,
    )
    assert result.verdict == "blocked"
    assert result.abstention_reason == "fabrication"
    assert result.needs_human_review
    assert [c.claim.value for c in result.fabrications] == ["12"]


def test_one_fabrication_among_correct_claims_still_blocks(monitor_settings, embedder):
    """Averaging is the failure mode this design exists to prevent."""
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m. The cylinder head bolts go to "
        "40 Nm. The transfer case nut is 250 Nm.",
        monitor_settings,
        embedder,
    )
    assert result.verdict == "blocked"
    assert result.faithfulness == pytest.approx(2 / 3)
    assert [c.claim.value for c in result.fabrications] == ["250"]


def test_a_judge_cannot_clear_a_fabrication(monitor_settings, embedder):
    """Asymmetry check: the model's opinion never overrides the lexical gate."""
    approving = JudgeVerdict(
        supported=True,
        insufficient_context=False,
        rubric_conflict=False,
        semantic_consistency=1.0,
        reasoning="looks right to me",
    )
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 12 N·m.",
        monitor_settings,
        embedder,
        judge_verdict=approving,
    )
    assert result.verdict == "blocked"


def test_judge_can_raise_an_abstention_on_a_lexically_clean_output(monitor_settings, embedder):
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m.",
        monitor_settings,
        embedder,
        judge_verdict=JudgeVerdict(
            supported=False,
            insufficient_context=True,
            rubric_conflict=False,
            semantic_consistency=0.9,
            reasoning="the source covers a different engine variant",
        ),
    )
    assert result.verdict == "abstained"
    assert result.abstention_reason == "insufficient_context"
    assert result.needs_human_review


def test_unavailable_judge_routes_to_review_rather_than_approving(monitor_settings, embedder):
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m.",
        monitor_settings,
        embedder,
        judge_verdict=JudgeVerdict.unavailable("no API key"),
    )
    assert result.needs_human_review
    assert result.verdict != "grounded"


def test_self_contradiction_is_a_rubric_conflict(monitor_settings, embedder):
    result = _monitor(
        "The torque specification is not available for this vehicle. Tighten the camshaft "
        "position sensor bolt to 9 N·m.",
        monitor_settings,
        embedder,
    )
    assert result.verdict == "abstained"
    assert result.abstention_reason == "rubric_conflict"


def test_claims_with_no_sources_abstain(monitor_settings, embedder):
    result = _monitor(
        "Tighten the bolt to 9 N·m.", monitor_settings, embedder, sources={}
    )
    assert result.verdict == "abstained"
    assert result.abstention_reason == "insufficient_context"


def test_citing_a_source_that_was_never_supplied_escalates(monitor_settings, embedder):
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m. [manual#c1][fsm-2004#p88]",
        monitor_settings,
        embedder,
        cited_ids=["manual#c1", "fsm-2004#p88"],
    )
    assert result.verdict == "escalated"
    assert result.citation_accuracy == 0.5
    assert result.needs_human_review


def test_low_semantic_consistency_abstains(monitor_settings, embedder):
    settings = monitor_settings.model_copy(update={"monitor_min_semantic": 0.99})
    result = _monitor(
        "Tighten the camshaft position sensor bolt to 9 N·m.", settings, embedder
    )
    assert result.verdict == "abstained"
    assert result.abstention_reason == "low_semantic_consistency"


def test_output_without_claims_is_not_counted_as_grounded(monitor_settings, embedder):
    result = _monitor(
        "The specification was not found in the indexed documentation. Consult the OEM "
        "service information.",
        monitor_settings,
        embedder,
    )
    assert result.verdict == "no_claims"


# --- harvesting -----------------------------------------------------------


def test_sources_are_harvested_from_tool_results():
    payload = {
        "specs": [
            {
                "value": "9",
                "verbatim_source_text": "tighten to 9 N·m",
                "citation": {"chunk_id": "c1", "source": "manual.pdf"},
            }
        ],
        "supporting_text": [
            {"text": "cylinder head bolts to 40 Nm", "citation": {"chunk_id": "c2"}}
        ],
        "instruction": "Report values exactly as extracted.",
    }
    sources = sources_from_tool_results([payload])
    assert sources == {"c1": "tighten to 9 N·m", "c2": "cylinder head bolts to 40 Nm"}
    # Our own instruction text is not evidence.
    assert "Report values exactly" not in "".join(sources.values())


def test_task_type_follows_the_last_grounding_tool():
    assert infer_task_type(["decode_vin", "lookup_torque_spec"]) == "torque_spec"
    assert infer_task_type(["decode_vin"]) == "vin_decode"
    assert infer_task_type([]) == "general"


# --- the store ------------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path, monitor_settings, embedder) -> MonitorStore:
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.record(
        _monitor("Tighten the bolt to 9 N·m.", monitor_settings, embedder),
        question="cam sensor torque",
        output="Tighten the bolt to 9 N·m.",
        vehicle="2004 INFINITI G35",
    )
    store.record(
        _monitor("Tighten the bolt to 12 N·m.", monitor_settings, embedder),
        question="cam sensor torque",
        output="Tighten the bolt to 12 N·m.",
        vehicle="2004 INFINITI G35",
    )
    return store


def test_store_summary_counts_verdicts(populated_store):
    summary = populated_store.summary()
    assert summary["events"] == 2
    assert summary["verdicts"]["grounded"] == 1
    assert summary["verdicts"]["blocked"] == 1
    assert summary["hallucination_rate"] == 0.5
    assert summary["fabrications"] == 1


def test_empty_store_reports_none_not_zero(tmp_path):
    """A 0% hallucination rate over no data would be a false safety claim."""
    summary = MonitorStore(tmp_path / "empty.sqlite3").summary()
    assert summary["events"] == 0
    assert summary["hallucination_rate"] is None
    assert summary["grounding_rate"] is None


def test_review_queue_and_resolution(populated_store):
    queue = populated_store.review_queue()
    assert len(queue) == 1
    event_id = queue[0]["id"]

    assert populated_store.resolve(event_id, reviewer="tech-7", outcome="rejected", note="wrong")
    assert populated_store.review_queue() == []

    event = populated_store.get(event_id)
    # The original verdict survives resolution — the record is not rewritten.
    assert event["verdict"] == "blocked"
    assert event["review_outcome"] == "rejected"
    assert event["reviewer"] == "tech-7"


def test_resolve_rejects_an_unknown_outcome(populated_store):
    with pytest.raises(ValueError, match="outcome must be one of"):
        populated_store.resolve("whatever", reviewer="t", outcome="looks-fine")


def test_by_task_type_breaks_the_rates_out(populated_store):
    rows = {r["task_type"]: r for r in populated_store.by_task_type()}
    assert rows["torque_spec"]["events"] == 2
    assert rows["torque_spec"]["hallucination_rate"] == 0.5
    assert rows["torque_spec"]["grounding_rate"] == 0.5


def test_fabrication_report_names_the_offending_value(populated_store):
    entries = populated_store.fabrications()
    assert len(entries) == 1
    assert [c["value"] for c in entries[0]["claims"]] == ["12"]


# --- the dashboard --------------------------------------------------------


def test_dashboard_renders_without_network_assets(populated_store):
    from aad.monitor.dashboard import render_from_store

    html = render_from_store(populated_store)
    assert "<title>Grounding monitor" in html
    assert "Hallucination rate" in html
    assert "torque_spec" in html
    # Self-contained: nothing to fetch, so it renders on a disconnected terminal.
    assert "http://" not in html
    assert "<script" not in html


def test_dashboard_handles_an_empty_store(tmp_path):
    from aad.monitor.dashboard import render_from_store

    html = render_from_store(MonitorStore(tmp_path / "empty.sqlite3"))
    assert "No monitored outputs yet" in html
    assert "—" in html  # not "0.0%"


def test_dashboard_escapes_recorded_text(tmp_path, monitor_settings, embedder):
    from aad.monitor.dashboard import render_from_store

    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.record(
        _monitor("Tighten the bolt to 9 N·m.", monitor_settings, embedder),
        question="<script>alert('x')</script>",
        output="Tighten the bolt to 9 N·m.",
    )
    html = render_from_store(store)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_semantic_score_is_measured_against_a_passage_not_the_whole_document(embedder):
    """A one-line claim must not be penalised for the rest of a long chunk."""
    from aad.monitor.lsc import semantic_consistency

    padding = "\n".join(f"Step {i}: refit the undertray fasteners." for i in range(40))
    claim = "Tighten the camshaft position sensor bolt to 9 N·m."
    assert semantic_consistency(claim, [SOURCE + padding], embedder) > 0.5


def test_terse_but_grounded_answers_are_not_flooded_into_review(monitor_settings, embedder):
    """Calibration guard: the default floor must not abstain on a correct short answer."""
    result = _monitor("Torque it to 9 N·m.", monitor_settings, embedder)
    assert result.verdict == "grounded"
