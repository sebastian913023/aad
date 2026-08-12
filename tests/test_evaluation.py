"""The accuracy harness, and the relevance gate it exposed.

The gate tests are the important ones. Retrieval returns the closest chunks for a
vehicle whether or not the requested component appears in them, so without a gate a
component absent from the corpus picks up the nearest unrelated torque value and
presents it with a citation. That is the exact failure this system exists to prevent,
and it shipped until the harness caught it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aad.errors import AadError
from aad.evaluation import EvalReport, GoldCase, load_goldset, run_evaluation
from aad.models import Vehicle
from aad.providers.labor import extract_labor_times
from aad.providers.torque import extract_torque_specs, lookup_torque_spec

GOLDSET = Path(__file__).resolve().parents[1] / "data" / "eval" / "sample_goldset.jsonl"

MANUAL = (
    "Installation is the reverse of removal. Tighten the camshaft position sensor "
    "retaining bolt to 9 Nm using bolt size M6 x 1.0."
)
HEAD = "Stage 1: torque all bolts to 40 Nm in sequence shown. Bolt size M11 x 1.5."


# --- the relevance gate ---------------------------------------------------
def test_extraction_requires_the_component_to_be_named():
    """A component absent from the text must yield nothing, not the nearest value."""
    assert extract_torque_specs(MANUAL, "camshaft position sensor retaining bolt")
    assert extract_torque_specs(MANUAL, "transfer case output shaft nut") == []


def test_substring_collisions_do_not_count_as_a_match():
    """'shaft' must not match inside 'camshaft'. This exact collision made a transfer
    case query return the camshaft sensor's 9 Nm."""
    assert extract_torque_specs(MANUAL, "output shaft nut") == []
    assert extract_torque_specs(MANUAL, "case bolt") == []


def test_section_heading_supplies_context_for_bare_table_rows():
    """Manuals put the component in a heading and the value in a row beneath it."""
    assert extract_torque_specs(HEAD, "cylinder head bolts") == []
    with_heading = extract_torque_specs(
        HEAD, "cylinder head bolts", "CYLINDER HEAD - TIGHTENING SEQUENCE"
    )
    assert with_heading and with_heading[0]["value"] == 40.0


def test_generic_fastener_words_alone_are_not_a_match():
    """Every torque spec in a manual contains 'bolt' and 'torque'. Matching on those
    alone identifies nothing."""
    assert extract_torque_specs(MANUAL, "differential pinion bolt") == []


def test_query_of_only_generic_words_still_returns_candidates():
    """When the caller genuinely has nothing specific to go on, fall back rather than
    returning an empty result for a query the corpus can partly answer."""
    assert extract_torque_specs(MANUAL, "bolts")


def test_labor_extraction_is_gated_the_same_way():
    text = "Labor time for camshaft position sensor replacement, bank 1: 0.7 hrs."
    assert extract_labor_times(text, "camshaft position sensor replacement")
    assert extract_labor_times(text, "transmission fluid service") == []


def test_lookup_reports_a_gap_for_an_absent_component(indexed_retriever, settings, g35):
    """End to end: the vehicle is indexed, the component is not."""
    result = lookup_torque_spec("transfer case output shaft nut", g35, indexed_retriever, settings)
    assert result["specs"] == []
    assert "unavailable_reason" in result
    assert "do not estimate" in result["instruction"].lower()


def test_lookup_still_answers_a_present_component(indexed_retriever, settings, g35):
    """The gate must not suppress real answers."""
    result = lookup_torque_spec(
        "camshaft position sensor retaining bolt", g35, indexed_retriever, settings
    )
    assert result["specs"][0]["value"] == 9.0


# --- gold set loading -----------------------------------------------------
def test_sample_goldset_loads_and_is_labelled():
    cases = load_goldset(GOLDSET)
    assert len(cases) >= 8
    assert any(c.must_abstain for c in cases), "a gold set with no abstention cases proves nothing"
    for case in cases:
        assert "HARNESS SELF-TEST" in case.notes, f"{case.id} is not marked as a self-test case"


def test_goldset_accepts_a_json_array(tmp_path: Path):
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "vehicle": {"year": 2004, "make": "INFINITI", "model": "G35"},
                    "query": "x",
                    "expected_value": 9,
                    "expected_unit": "Nm",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert len(load_goldset(path)) == 1


def test_duplicate_case_ids_are_rejected(tmp_path: Path):
    path = tmp_path / "gold.jsonl"
    row = {
        "id": "dup",
        "vehicle": {"year": 2004, "make": "INFINITI", "model": "G35"},
        "query": "x",
        "must_abstain": True,
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(AadError, match="duplicate case ids"):
        load_goldset(path)


def test_empty_goldset_is_rejected(tmp_path: Path):
    path = tmp_path / "gold.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(AadError, match="empty"):
        load_goldset(path)


# --- scoring --------------------------------------------------------------
def _run(cases, retriever, settings) -> EvalReport:
    return run_evaluation(cases, retriever=retriever, settings=settings)


def test_correct_value_scores_correct(indexed_retriever, settings, g35):
    case = GoldCase(
        id="c",
        vehicle=g35,
        query="camshaft position sensor retaining bolt",
        expected_value=9,
        expected_unit="Nm",
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "correct"
    assert report.hallucination_rate == 0.0
    assert report.citation_coverage == 1.0


def test_unit_conversion_is_applied_for_comparison(indexed_retriever, settings, g35):
    """9 Nm is 6.64 lb-ft. A gold value in either unit must score against a corpus
    stating the other."""
    case = GoldCase(
        id="c",
        vehicle=g35,
        query="camshaft position sensor retaining bolt",
        expected_value=6.64,
        expected_unit="lb-ft",
        tolerance=0.03,
    )
    assert _run([case], indexed_retriever, settings).results[0].outcome == "correct"


def test_wrong_value_scores_wrong_not_correct(indexed_retriever, settings, g35):
    case = GoldCase(
        id="c",
        vehicle=g35,
        query="camshaft position sensor retaining bolt",
        expected_value=25,
        expected_unit="Nm",
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "wrong"
    assert report.hallucination_rate == 1.0
    assert report.precision == 0.0


def test_correct_abstention_counts_toward_accuracy(indexed_retriever, settings):
    case = GoldCase(
        id="c",
        vehicle=Vehicle(year=1998, make="Saab", model="900"),
        query="cylinder head bolts",
        must_abstain=True,
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "abstained"
    assert report.accuracy == 1.0, "abstaining correctly is a right answer, not a non-answer"
    assert report.hallucination_rate == 0.0


def test_answering_a_must_abstain_case_is_a_hallucination(indexed_retriever, settings, g35):
    case = GoldCase(
        id="c", vehicle=g35, query="camshaft position sensor retaining bolt", must_abstain=True
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "wrong"
    assert report.hallucination_rate == 1.0


def test_missing_an_answerable_question_is_safe_but_scored(indexed_retriever, settings, g35):
    case = GoldCase(
        id="c",
        vehicle=g35,
        query="transfer case output shaft nut",
        expected_value=50,
        expected_unit="Nm",
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "missed"
    assert report.hallucination_rate == 0.0  # a miss is not a hallucination
    assert report.accuracy == 0.0


def test_retrieval_recall_is_scored_against_the_expected_source(
    indexed_retriever, settings, g35
):
    case = GoldCase(
        id="c",
        kind="retrieval",
        vehicle=g35,
        query="camshaft position sensor removal",
        expected_source="sample_service_manual.md",
    )
    report = _run([case], indexed_retriever, settings)
    assert report.results[0].outcome == "correct"
    assert report.retrieval_recall == 1.0


def test_full_sample_goldset_has_no_hallucinations(indexed_retriever, settings):
    """The regression that matters: the harness caught a real leak here, and this
    keeps it caught."""
    report = _run(load_goldset(GOLDSET), indexed_retriever, settings)
    assert report.hallucination_rate == 0.0, [
        (r.id, r.detail, r.actual) for r in report.results if r.outcome == "wrong"
    ]
    assert report.count("error") == 0
    assert report.citation_coverage == 1.0
