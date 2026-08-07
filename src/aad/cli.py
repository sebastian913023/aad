"""Unified `aad` command line.

    aad ingest add|stats|remove   index service documentation
    aad serve                     run the API
    aad check                     run the test suite and linter
    aad doctor                    live-check every configured provider
    aad verify-index              report whether the corpus is fit to serve
    aad monitor ...               grounding metrics, dashboard, human-review queue

`doctor` and `verify-index` exist because "the tests pass" and "this is safe to put
in front of a technician" are different questions. The first is about code; the
second is about whether the indexed corpus is real, licensed, vehicle-scoped
material — which no unit test can tell you.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from aad.config import get_settings
from aad.ingest.cli import build_parser as build_ingest_parser
from aad.ingest.cli import main as ingest_main
from aad.rag.store import LocalVectorStore, get_vector_store

EXIT_OK = 0
EXIT_FAIL = 1


# --- doctor ---------------------------------------------------------------


def _check_nhtsa_vpic(timeout: float) -> tuple[bool, str]:
    """Decode a known VIN against the live vPIC service and verify the result.

    A reachable-but-wrong response is a failure: the point is to confirm the
    decode still maps to the fields the retriever scopes on, not just that the
    host answers.
    """
    import httpx

    from aad.providers.vin import VPIC_URL

    vin = "1M8GDM9AXKP042788"  # NHTSA's own documented example VIN
    try:
        response = httpx.get(VPIC_URL.format(vin=vin), params={"format": "json"}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return False, f"unreachable: {type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    results = (response.json() or {}).get("Results") or []
    if not results:
        return False, "reachable but returned no Results"

    record = results[0]
    missing = [f for f in ("Make", "ModelYear") if not record.get(f)]
    if missing:
        return False, f"reachable but missing fields: {', '.join(missing)}"
    return True, f"decoded {vin} -> {record.get('ModelYear')} {record.get('Make')}"


def _check_nhtsa_recalls(timeout: float) -> tuple[bool, str]:
    import httpx

    from aad.providers.tsb import RECALLS_URL

    try:
        response = httpx.get(
            RECALLS_URL,
            params={"make": "honda", "model": "accord", "modelYear": 2018},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"unreachable: {type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    if "results" not in (response.json() or {}):
        return False, "reachable but response has no 'results' key"
    return True, "recall query returned a well-formed response"


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    failures = 0

    print("live provider checks")
    print("--------------------")
    for name, check in (
        ("NHTSA vPIC (VIN decode)", _check_nhtsa_vpic),
        ("NHTSA recalls", _check_nhtsa_recalls),
    ):
        ok, detail = check(args.timeout)
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures += 1

    print("\ncommercial providers")
    print("--------------------")
    from aad.providers.profiles import load_profiles

    env = settings.provider_env()
    for name, profile in sorted(load_profiles(settings.provider_profile_dir).items()):
        if not profile.is_configured(env):
            print(f"  [--  ] {name}: not configured — set {', '.join(profile.missing(env))}")
        elif not profile.verified:
            print(f"  [warn] {name}: configured but UNVERIFIED — run `aad providers test {name}`")
        else:
            print(f"  [ok  ] {name}: {profile.vendor}")

    print("\nmodel")
    print("-----")
    print(f"  provider: {settings.provider}")
    print(f"  model:    {settings.model} (effort: {settings.model_effort})")

    if failures:
        print(f"\n{failures} live check(s) failed.", file=sys.stderr)
    return EXIT_FAIL if failures else EXIT_OK


# --- verify-index ---------------------------------------------------------


def cmd_verify_index(args: argparse.Namespace) -> int:
    """Report whether the indexed corpus is fit to serve.

    Production readiness is not a code property. This inspects what is actually
    indexed and refuses anything that would put invented or unscoped values in
    front of a technician.
    """
    settings = get_settings()
    store = get_vector_store(settings)

    if not isinstance(store, LocalVectorStore):
        print("verify-index currently supports the local vector store only.", file=sys.stderr)
        return EXIT_FAIL

    total = store.count()
    chunks = store._chunks
    synthetic = [c for c in chunks if c.synthetic]
    unscoped = [c for c in chunks if not (c.year and c.make and c.model)]
    spec_bearing = [c for c in chunks if c.spec_type in {"torque_spec", "labor_time", "wiring_diagram"}]

    synthetic_sources = sorted({c.source for c in synthetic})
    unscoped_sources = sorted({c.source for c in unscoped})

    print(f"index:            {settings.index_dir}")
    print(f"chunks:           {total}")
    print(f"spec-bearing:     {len(spec_bearing)} (torque / labor / wiring)")
    print(f"synthetic:        {len(synthetic)} across {len(synthetic_sources)} source(s)")
    print(f"unscoped:         {len(unscoped)} across {len(unscoped_sources)} source(s)")

    problems: list[str] = []
    if total == 0:
        problems.append("index is empty — nothing to serve")
    if synthetic:
        problems.append(
            f"{len(synthetic)} synthetic chunk(s) from: {', '.join(synthetic_sources)}. "
            "These carry invented values and must not back a production deployment."
        )
    if unscoped:
        problems.append(
            f"{len(unscoped)} chunk(s) lack year/make/model from: {', '.join(unscoped_sources)}. "
            "Correct for generic references; wrong for model-specific service data."
        )

    if not args.production:
        print("\n(development check — pass --production for the readiness gate)")
        for problem in problems:
            print(f"  note: {problem}")
        return EXIT_OK

    print()
    if problems:
        print("NOT READY FOR PRODUCTION USE", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nIndex licensed OEM service documentation and re-run. Synthetic and "
            "unscoped material must be removed with `aad ingest remove <source>`.",
            file=sys.stderr,
        )
        return EXIT_FAIL

    print("index contains only vehicle-scoped, non-synthetic material.")
    print(
        "This checks provenance flags, not licensing. Confirm separately that every "
        "indexed document is covered by your shop's subscription."
    )
    return EXIT_OK


# --- providers ------------------------------------------------------------


def _profiles(args: argparse.Namespace):
    from aad.providers.profiles import load_profiles

    settings = get_settings()
    return load_profiles(getattr(args, "profile_dir", None) or settings.provider_profile_dir)


def cmd_providers_list(args: argparse.Namespace) -> int:
    profiles = _profiles(args)
    print(f"{'provider':<10} {'configured':<11} {'verified':<9} vendor")
    print(f"{'-' * 10} {'-' * 11} {'-' * 9} {'-' * 40}")
    for name in sorted(profiles):
        profile = profiles[name]
        configured = "yes" if profile.is_configured() else "no"
        verified = "yes" if profile.verified else "NO"
        print(f"{name:<10} {configured:<11} {verified:<9} {profile.vendor}")

    unconfigured = [p for p in profiles.values() if not p.is_configured()]
    if unconfigured:
        print("\nto configure:")
        for profile in unconfigured:
            print(f"  {profile.name}: set {', '.join(profile.missing())}")

    unverified = [p for p in profiles.values() if p.is_configured() and not p.verified]
    if unverified:
        print(
            "\nConfigured but unverified: the request/response mapping in these profiles "
            "is a placeholder, not a contract anyone has exercised. Run "
            "`aad providers test <name>` against the real service and set "
            '"verified": true only once the mapping is confirmed.'
        )
    return EXIT_OK


def cmd_providers_show(args: argparse.Namespace) -> int:
    from aad.providers.client import build_request
    from aad.providers.profiles import get_profile

    settings = get_settings()
    profile = get_profile(args.name, getattr(args, "profile_dir", None) or settings.provider_profile_dir)

    print(f"provider:   {profile.name}")
    print(f"vendor:     {profile.vendor}")
    print(f"docs:       {profile.docs_url or '(none recorded)'}")
    print(f"verified:   {'yes' if profile.verified else 'NO — mapping is a placeholder'}")
    print(f"endpoint:   {profile.method} {{{profile.base_url_env}}}{profile.path}")
    print(f"auth:       {profile.auth.type}" + (f" ({profile.auth.name})" if profile.auth.name else ""))
    print(f"credential: {profile.credential_env}")
    print(f"configured: {'yes' if profile.is_configured() else 'no — set ' + ', '.join(profile.missing())}")

    problems = profile.validate_shape()
    if problems:
        print("\nprofile problems:")
        for problem in problems:
            print(f"  - {problem}")

    print("\nrequest parameters (wire name <- our field):")
    for wire, ours in sorted(profile.params.items()):
        print(f"  {wire:<12} <- {ours}")

    print("\nresponse mapping (our field <- response path):")
    print(f"  results list <- {profile.results_path or '(response body)'}")
    for ours, path in sorted(profile.field_map.items()):
        print(f"  {ours:<16} <- {path}")

    if profile.is_configured():
        sample = build_request(
            profile,
            {
                "year": 2004,
                "make": "INFINITI",
                "model": "G35",
                "engine": "3.5L V6",
                "component": "cylinder head bolts",
                "operation": "replace water pump",
                "query": "water pump",
                "circuit": "camshaft position sensor",
                "limit": 10,
            },
        )
        redacted = dict(sample)
        redacted["headers"] = {
            k: ("***redacted***" if k.lower() == "authorization" else v)
            for k, v in sample["headers"].items()
        }
        print("\nexample request (credential redacted):")
        print(f"  {redacted['method']} {redacted['url']}")
        print(f"  headers: {redacted['headers']}")
        print(f"  params:  {redacted.get('params') or redacted.get('json')}")

    return EXIT_FAIL if problems else EXIT_OK


def cmd_providers_test(args: argparse.Namespace) -> int:
    """Make one real call and report exactly what came back.

    This is the step that turns `verified: false` into a decision you can defend:
    it shows the mapped result next to the raw payload, so a silent field-name
    mismatch is visible rather than showing up later as a missing torque unit.
    """
    import json as _json

    from aad.errors import AadError
    from aad.providers.client import build_request, call_provider
    from aad.providers.profiles import get_profile

    settings = get_settings()
    profile = get_profile(args.name, getattr(args, "profile_dir", None) or settings.provider_profile_dir)

    if not profile.is_configured():
        print(
            f"{profile.name} is not configured: set {', '.join(profile.missing())}",
            file=sys.stderr,
        )
        return EXIT_FAIL

    source = {
        "year": args.year,
        "make": args.make,
        "model": args.model,
        "engine": args.engine,
        "vin": args.vin,
        "component": args.arg,
        "operation": args.arg,
        "query": args.arg,
        "circuit": args.arg,
        "scan_id": args.arg,
        "limit": 5,
    }

    request = build_request(profile, source)
    print(f"{request['method']} {request['url']}")
    print(f"params: {request.get('params') or request.get('json')}\n")

    try:
        results = call_provider(profile, source)
    except AadError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAIL

    if not results:
        print("Call succeeded but returned zero results.")
        print(
            "That may be correct for this vehicle, or the profile's results_path may be "
            "wrong. Try a vehicle you know has data before trusting the mapping."
        )
        return EXIT_FAIL

    print(f"{len(results)} result(s), mapped through the profile's field_map:\n")
    print(_json.dumps(results[: args.limit], indent=2, default=str))

    empty = [k for k, v in results[0].items() if v is None]
    unmapped = sorted(set(profile.field_map) - set(results[0]))
    if unmapped:
        print(
            f"\nFields the mapping produced nothing for: {', '.join(unmapped)}. "
            "Either the vendor does not return them, or the field_map paths are wrong."
        )
    if not unmapped and not empty:
        print("\nEvery mapped field was populated. If the values look right, set "
              f'"verified": true in the {profile.name} profile.')
    return EXIT_OK


# --- eval -----------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    """Score the system against a gold set drawn from the indexed corpus.

    Refuses to report accuracy over synthetic material unless explicitly forced.
    A number computed against invented specifications reads as evidence and is
    not evidence, and that is a worse failure than having no number.
    """
    import json as _json

    from aad.errors import AadError
    from aad.evaluation import load_goldset, run_evaluation
    from aad.rag.store import LocalVectorStore, get_vector_store

    settings = get_settings()

    try:
        cases = load_goldset(args.goldset)
    except (AadError, OSError, ValueError) as exc:
        print(f"could not load gold set: {exc}", file=sys.stderr)
        return EXIT_FAIL

    store = get_vector_store(settings)
    synthetic = 0
    sources: dict[str, int] = {}
    if isinstance(store, LocalVectorStore):
        sources = store.sources()
        synthetic = sum(1 for c in store._chunks if c.synthetic)

    if synthetic and not args.allow_synthetic:
        print("REFUSING TO REPORT ACCURACY", file=sys.stderr)
        print(
            f"\nThe index contains {synthetic} synthetic chunk(s). An accuracy figure "
            "measured against invented specifications would look like evidence while "
            "meaning nothing.\n\n"
            "Index licensed OEM documentation, confirm with `aad verify-index --production`, "
            "then re-run. To exercise the harness itself on sample data (results are not an "
            "accuracy claim), pass --allow-synthetic.",
            file=sys.stderr,
        )
        return EXIT_FAIL

    report = run_evaluation(cases, settings=settings)
    report.corpus_sources = sources
    report.synthetic_chunks = synthetic

    if args.json:
        print(_json.dumps(report.to_dict(), indent=2))
    else:
        if synthetic:
            print("*** HARNESS SELF-TEST ON SYNTHETIC DATA — NOT AN ACCURACY RESULT ***\n")
        print(f"cases:              {report.total}")
        print(f"  correct:          {report.count('correct')}")
        print(f"  wrong:            {report.count('wrong')}")
        print(f"  abstained:        {report.count('abstained')}  (correctly said unavailable)")
        print(f"  missed:           {report.count('missed')}  (abstained but answer was present)")
        print(f"  error:            {report.count('error')}")
        print()
        print(f"accuracy:           {report.accuracy:.1%}")
        print(f"precision:          {report.precision:.1%}  (of those it chose to answer)")
        print(f"hallucination rate: {report.hallucination_rate:.1%}  <- gate metric")
        print(f"abstention rate:    {report.abstention_rate:.1%}")
        print(f"citation coverage:  {report.citation_coverage:.1%}")
        if report.retrieval_recall is not None:
            print(f"retrieval recall:   {report.retrieval_recall:.1%}")

        failures = [r for r in report.results if r.outcome in ("wrong", "error")]
        if failures:
            print("\nfailures:")
            for r in failures:
                print(f"  [{r.outcome}] {r.id}: {r.detail}")
                if r.expected or r.actual:
                    print(f"      expected {r.expected!r}, got {r.actual!r}")

    gate_failures: list[str] = []
    if report.hallucination_rate > args.max_hallucination_rate:
        gate_failures.append(
            f"hallucination rate {report.hallucination_rate:.1%} exceeds the limit "
            f"{args.max_hallucination_rate:.1%}"
        )
    if report.citation_coverage < args.min_citation_coverage:
        gate_failures.append(
            f"citation coverage {report.citation_coverage:.1%} is below the required "
            f"{args.min_citation_coverage:.1%}"
        )
    if report.accuracy < args.min_accuracy:
        gate_failures.append(
            f"accuracy {report.accuracy:.1%} is below the required {args.min_accuracy:.1%}"
        )

    if gate_failures:
        print("\nEVALUATION GATE FAILED", file=sys.stderr)
        for failure in gate_failures:
            print(f"  - {failure}", file=sys.stderr)
        return EXIT_FAIL

    if not args.json:
        print("\nevaluation gates passed")
    return EXIT_OK


# --- monitor --------------------------------------------------------------


def _monitor_store():
    from aad.monitor.store import get_monitor_store

    return get_monitor_store()


def cmd_monitor_summary(args: argparse.Namespace) -> int:
    import json as _json
    import time as _time

    store = _monitor_store()
    since = _time.time() - args.since_hours * 3600 if args.since_hours else None
    summary = store.summary(since=since)
    by_task = store.by_task_type(since=since)
    reasons = store.abstention_reasons(since=since)

    if args.json:
        print(
            _json.dumps(
                {"summary": summary, "by_task_type": by_task, "abstention_reasons": reasons},
                indent=2,
            )
        )
        return EXIT_OK

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    if not summary["events"]:
        print("no monitored outputs recorded yet")
        print(f"(store: {store.path})")
        return EXIT_OK

    print(f"monitored outputs:   {summary['events']}  (claim-bearing: {summary['claim_events']})")
    print(f"hallucination rate:  {pct(summary['hallucination_rate'])}")
    print(f"grounding rate:      {pct(summary['grounding_rate'])}")
    print(f"abstention rate:     {pct(summary['abstention_rate'])}")
    print(f"fabricated claims:   {summary['fabrications']} of {summary['claims']}")
    print(f"mean faithfulness:   {summary['mean_faithfulness']}")
    print(f"citation accuracy:   {summary['mean_citation_accuracy']}")
    print(f"semantic consistency: {summary['mean_semantic_consistency']}")
    print(f"awaiting review:     {summary['pending_review']}")

    if by_task:
        print("\nby task type:")
        for row in by_task:
            print(
                f"  {row['task_type']:<16} events={row['events']:<5} "
                f"grounded={pct(row['grounding_rate']):<7} "
                f"abstained={pct(row['abstention_rate']):<7} "
                f"hallucinated={pct(row['hallucination_rate'])}"
            )
    if reasons:
        print("\nabstention triggers:")
        for name, count in reasons.items():
            print(f"  {name:<26} {count}")

    # A non-zero hallucination rate is a build-breaking condition, not a statistic.
    if summary["hallucination_rate"]:
        print("\nHALLUCINATIONS RECORDED — see `aad monitor queue`", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def cmd_monitor_dashboard(args: argparse.Namespace) -> int:
    from aad.monitor.dashboard import dashboard_json, render_from_store

    store = _monitor_store()
    if args.json:
        payload = dashboard_json(store)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(payload)
        return EXIT_OK

    html = render_from_store(store)
    out = Path(args.out or "monitor-dashboard.html")
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(html)} bytes) — open it in a browser, no server needed")
    return EXIT_OK


def cmd_monitor_queue(args: argparse.Namespace) -> int:
    queue = _monitor_store().review_queue(limit=args.limit)
    if not queue:
        print("review queue is empty")
        return EXIT_OK
    for event in queue:
        reason = event["abstention_reason"] or "unconfirmed"
        print(f"{event['id']}  {event['verdict']:<10} {event['task_type']:<14} {reason}")
        print(f"    vehicle:  {event['vehicle'] or '-'}")
        print(f"    question: {(event['question'] or '')[:100]}")
        for claim in event["payload"].get("claims", []):
            if claim.get("fabricated"):
                print(f"    FABRICATED: {claim.get('value')}{claim.get('unit') or ''} "
                      f"({claim.get('type')})")
    print(f"\n{len(queue)} output(s) awaiting review")
    return EXIT_OK


def cmd_monitor_resolve(args: argparse.Namespace) -> int:
    ok = _monitor_store().resolve(
        args.event_id, reviewer=args.reviewer, outcome=args.outcome, note=args.note
    )
    if not ok:
        print(f"no monitor event {args.event_id!r}", file=sys.stderr)
        return EXIT_FAIL
    print(f"{args.event_id} resolved as {args.outcome} by {args.reviewer}")
    return EXIT_OK


def cmd_monitor_verify(args: argparse.Namespace) -> int:
    """Verify a written-down output against source files on disk.

    Useful outside the agent loop: paste any answer and the documents it claims to
    rest on, and get the same zero-tolerance verdict the live path applies.
    """
    import json as _json

    from aad.monitor.pipeline import monitor_output

    sources: dict[str, str] = {}
    for spec in args.source:
        source_id, _, path = spec.partition("=")
        if not path:
            source_id, path = Path(spec).name, spec
        try:
            sources[source_id] = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not read source {path!r}: {exc}", file=sys.stderr)
            return EXIT_FAIL

    try:
        output = Path(args.output).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read output {args.output!r}: {exc}", file=sys.stderr)
        return EXIT_FAIL

    result = monitor_output(
        question=args.question,
        output=output,
        sources=sources,
        task_type=args.task_type,
        use_judge=args.judge,
        settings=get_settings(),
    )
    if args.record:
        event_id = _monitor_store().record(result, question=args.question, output=output)
        print(f"recorded as {event_id}", file=sys.stderr)

    if args.json:
        print(_json.dumps(result.to_dict(), indent=2))
    else:
        print(f"verdict:            {result.verdict}")
        print(f"claims checked:     {len(result.claims)}")
        print(f"faithfulness:       {result.faithfulness:.2f}")
        print(f"citation accuracy:  {result.citation_accuracy:.2f}")
        print(f"semantic consistency: {result.mean_semantic:.2f}")
        if result.abstention_reason:
            print(f"abstention trigger: {result.abstention_reason}")
        for note in result.notes:
            print(f"  note: {note}")
        for report in result.claims:
            mark = "FABRICATED" if report.fabricated else "grounded  "
            print(
                f"  [{mark}] {report.claim.claim_type}: "
                f"{report.claim.value}{report.claim.unit or ''} — {report.score.detail}"
            )

    return EXIT_FAIL if result.verdict in {"blocked", "escalated"} else EXIT_OK


# --- serve / check --------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed; `pip install 'uvicorn[standard]'`", file=sys.stderr)
        return EXIT_FAIL

    uvicorn.run("aad.api:app", host=args.host, port=args.port, reload=args.reload)
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """Run the same gates CI runs, so a red PR is reproducible locally."""
    root = Path(__file__).resolve().parents[2]
    steps: list[tuple[str, list[str]]] = []

    # Look beside the running interpreter first: inside a venv that is where
    # ruff lives, and PATH often does not include it. Silently skipping lint
    # here would make `aad check` disagree with CI, which is the one thing this
    # command exists to prevent.
    venv_ruff = Path(sys.executable).parent / "ruff"
    ruff = str(venv_ruff) if venv_ruff.exists() else shutil.which("ruff")
    if ruff:
        steps.append(("lint", [ruff, "check", "src", "tests"]))
    else:
        print("ruff not found, skipping lint (pip install -e '.[dev]')", file=sys.stderr)

    pytest_cmd = [sys.executable, "-m", "pytest", "-q"]
    if args.live:
        pytest_cmd += ["-m", "live" if args.live_only else "live or not live"]
    steps.append(("tests", pytest_cmd))

    env = {**os.environ, **({"AAD_LIVE_TESTS": "1"} if args.live else {})}

    failed = 0
    for name, cmd in steps:
        print(f"\n=== {name}: {' '.join(cmd)} ===")
        # check=False: every step runs even when an earlier one fails, so one
        # invocation reports the full picture rather than the first problem.
        result = subprocess.run(cmd, cwd=root, env=env, check=False)
        if result.returncode != 0:
            failed += 1
            print(f"--- {name} FAILED (exit {result.returncode}) ---", file=sys.stderr)

    print("\nall checks passed" if not failed else f"\n{failed} check(s) failed")
    return EXIT_FAIL if failed else EXIT_OK


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aad", description="Auto mechanic diagnostic assistant."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="index service documentation", add_help=False)
    ingest.set_defaults(handler=None)  # delegated verbatim to the ingest CLI

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(handler=cmd_serve)

    check = sub.add_parser("check", help="run the linter and test suite (what CI runs)")
    check.add_argument("--live", action="store_true", help="also run live provider tests")
    check.add_argument("--live-only", action="store_true", help="run only live provider tests")
    check.set_defaults(handler=cmd_check)

    doctor = sub.add_parser("doctor", help="live-check provider reachability and configuration")
    doctor.add_argument("--timeout", type=float, default=20.0)
    doctor.set_defaults(handler=cmd_doctor)

    evaluate = sub.add_parser("eval", help="score accuracy against a gold set")
    evaluate.add_argument("--goldset", required=True, help="path to a JSONL or JSON gold set")
    evaluate.add_argument("--json", action="store_true", help="emit the full report as JSON")
    evaluate.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="exercise the harness on synthetic data (results are NOT an accuracy claim)",
    )
    evaluate.add_argument("--max-hallucination-rate", type=float, default=0.0)
    evaluate.add_argument("--min-citation-coverage", type=float, default=1.0)
    evaluate.add_argument("--min-accuracy", type=float, default=0.0)
    evaluate.set_defaults(handler=cmd_eval)

    providers = sub.add_parser("providers", help="inspect and test the commercial providers")
    providers.add_argument("--profile-dir", help="directory of JSON profile overrides")
    psub = providers.add_subparsers(dest="providers_command", required=True)

    plist = psub.add_parser("list", help="show configuration state for every provider")
    plist.set_defaults(handler=cmd_providers_list)

    pshow = psub.add_parser("show", help="show one profile and the request it would send")
    pshow.add_argument("name")
    pshow.set_defaults(handler=cmd_providers_show)

    ptest = psub.add_parser("test", help="make one real call and show the mapped result")
    ptest.add_argument("name")
    ptest.add_argument("--arg", default="water pump", help="component / operation / query / circuit")
    ptest.add_argument("--year", type=int, default=2018)
    ptest.add_argument("--make", default="Honda")
    ptest.add_argument("--model", default="Accord")
    ptest.add_argument("--engine", default=None)
    ptest.add_argument("--vin", default=None)
    ptest.add_argument("--limit", type=int, default=3)
    ptest.set_defaults(handler=cmd_providers_test)

    verify = sub.add_parser("verify-index", help="report whether the corpus is fit to serve")
    verify.add_argument(
        "--production",
        action="store_true",
        help="exit non-zero unless the corpus is production-ready",
    )
    verify.set_defaults(handler=cmd_verify_index)

    monitor = sub.add_parser("monitor", help="grounding monitor: metrics, dashboard, review queue")
    msub = monitor.add_subparsers(dest="monitor_command", required=True)

    msummary = msub.add_parser("summary", help="headline grounding metrics (non-zero exit if any "
                                              "hallucination was recorded)")
    msummary.add_argument("--since-hours", type=float, default=None)
    msummary.add_argument("--json", action="store_true")
    msummary.set_defaults(handler=cmd_monitor_summary)

    mdash = msub.add_parser("dashboard", help="write the self-contained HTML dashboard")
    mdash.add_argument("--out", default=None, help="output path (default monitor-dashboard.html)")
    mdash.add_argument("--json", action="store_true", help="emit the underlying data instead")
    mdash.set_defaults(handler=cmd_monitor_dashboard)

    mqueue = msub.add_parser("queue", help="outputs awaiting human review")
    mqueue.add_argument("--limit", type=int, default=25)
    mqueue.set_defaults(handler=cmd_monitor_queue)

    mresolve = msub.add_parser("resolve", help="close out one review")
    mresolve.add_argument("event_id")
    mresolve.add_argument("--reviewer", required=True)
    mresolve.add_argument(
        "--outcome", required=True, choices=["confirmed", "corrected", "rejected"]
    )
    mresolve.add_argument("--note", default="")
    mresolve.set_defaults(handler=cmd_monitor_resolve)

    mverify = msub.add_parser("verify", help="verify a written output against source files")
    mverify.add_argument("--output", required=True, help="file holding the output to verify")
    mverify.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="[ID=]PATH",
        help="a source the output cited; repeatable",
    )
    mverify.add_argument("--question", default="")
    mverify.add_argument("--task-type", default="general")
    mverify.add_argument("--judge", action="store_true", help="also run the judge model")
    mverify.add_argument("--record", action="store_true", help="log the result to the monitor db")
    mverify.add_argument("--json", action="store_true")
    mverify.set_defaults(handler=cmd_monitor_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `aad ingest ...` forwards its arguments untouched so the ingest CLI keeps
    # its own flags and help text.
    if argv and argv[0] == "ingest":
        return ingest_main(argv[1:])
    if argv and argv[0] in {"-h", "--help"}:
        build_parser().print_help()
        print("\ningest subcommands:\n")
        build_ingest_parser().print_help()
        return EXIT_OK

    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
