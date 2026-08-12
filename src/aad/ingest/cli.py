"""Ingestion CLI: `aad-ingest <path> [--year ... --make ... --model ... --engine ...]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aad.config import get_settings
from aad.ingest.pipeline import DocumentMeta, ingest_path
from aad.rag.store import LocalVectorStore, get_vector_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aad-ingest", description="Index service documentation.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="parse, chunk, embed and index a file or directory")
    add.add_argument("path", type=Path)
    add.add_argument("--year", type=int)
    add.add_argument("--make")
    add.add_argument("--model")
    add.add_argument("--engine")
    add.add_argument(
        "--append",
        action="store_true",
        help="keep existing chunks for these sources instead of replacing them",
    )

    sub.add_parser("stats", help="show what is currently indexed")

    remove = sub.add_parser("remove", help="drop every chunk from one source document")
    remove.add_argument("source", help="source filename as shown by `stats`")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "stats":
        store = get_vector_store(settings)
        print(f"index: {settings.index_dir}")
        print(f"chunks: {store.count()}")
        if isinstance(store, LocalVectorStore):
            for source, count in sorted(store.sources().items()):
                print(f"  {count:>6}  {source}")
        return 0

    if args.command == "remove":
        store = get_vector_store(settings)
        removed = store.delete_source(args.source)
        print(f"removed {removed} chunks from {args.source}")
        return 0

    meta = None
    if any([args.year, args.make, args.model, args.engine]):
        meta = DocumentMeta(year=args.year, make=args.make, model=args.model, engine=args.engine)

    report = ingest_path(args.path, meta=meta, settings=settings, replace=not args.append)

    print(f"files indexed : {report.files}")
    print(f"chunks written: {report.chunks_written} (of {report.chunks_seen} produced)")
    for spec_type, count in sorted(report.by_spec_type.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {spec_type}")
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)

    return 1 if report.errors and report.files == 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
