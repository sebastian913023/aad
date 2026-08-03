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

    print("\nconfigured optional providers")
    print("-----------------------------")
    optional = {
        "torque spec API": settings.torque_api_base,
        "labor time API": settings.labor_api_base,
        "parts catalog": settings.parts_api_base,
        "OBD2 scan service": settings.obd2_api_base,
        "wiring diagrams": settings.wiring_api_base,
    }
    for name, base in optional.items():
        state = "configured" if base else "not configured (lookups will report a gap)"
        print(f"  [{'ok  ' if base else '--  '}] {name}: {state}")

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
