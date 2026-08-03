"""Unified `aad` command line.

    aad ingest add|stats|remove   index service documentation
    aad serve                     run the API
    aad check                     run the test suite and linter
    aad doctor                    live-check every configured provider
    aad verify-index              report whether the corpus is fit to serve

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
