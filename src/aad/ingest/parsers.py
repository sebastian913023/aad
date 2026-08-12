"""Document parsers.

`LocalParser` handles text, markdown, and (when `pypdf` is installed) PDFs. It is the
default so the pipeline runs with no external service. `LlamaParseParser` is the
production path for scanned or heavily-tabular OEM manuals, where layout-aware parsing
is what keeps a torque table from collapsing into unusable prose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES


@dataclass(slots=True)
class ParsedPage:
    page: int
    text: str


class Parser(Protocol):
    def parse(self, path: Path) -> list[ParsedPage]: ...


class LocalParser:
    def parse(self, path: Path) -> list[ParsedPage]:
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            return self._parse_text(path)
        if suffix in PDF_SUFFIXES:
            return self._parse_pdf(path)
        raise ProviderError(f"unsupported file type: {path.suffix} ({path.name})")

    def _parse_text(self, path: Path) -> list[ParsedPage]:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Honor explicit form feeds as page breaks; otherwise treat the file as one page.
        pages = text.split("\f") if "\f" in text else [text]
        return [ParsedPage(page=i + 1, text=p) for i, p in enumerate(pages) if p.strip()]

    def _parse_pdf(self, path: Path) -> list[ParsedPage]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise ProviderError(
                "PDF parsing requires 'pypdf' (pip install pypdf), or set "
                "AAD_PARSER_BACKEND=llamaparse for layout-aware parsing"
            ) from exc

        reader = PdfReader(str(path))
        pages: list[ParsedPage] = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(ParsedPage(page=i + 1, text=text))
        if not pages:
            raise ProviderError(
                f"{path.name}: no extractable text (likely a scan). Use LlamaParse "
                "or OCR this document before indexing."
            )
        return pages


class LlamaParseParser:
    """Layout-aware parsing for tables, diagrams, and scanned pages."""

    BASE_URL = "https://api.cloud.llamaindex.ai/api/parsing"

    def __init__(self, api_key: str, timeout: float = 300.0, poll_interval: float = 3.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval

    def parse(self, path: Path) -> list[ParsedPage]:
        headers = {"Authorization": f"Bearer {self.api_key}", "accept": "application/json"}
        with httpx.Client(timeout=60.0) as client:
            with path.open("rb") as handle:
                upload = client.post(
                    f"{self.BASE_URL}/upload",
                    headers=headers,
                    files={"file": (path.name, handle, "application/octet-stream")},
                    data={"result_type": "markdown"},
                )
            if upload.status_code >= 400:
                raise ProviderError(f"LlamaParse upload failed ({upload.status_code}): {upload.text}")
            job_id = upload.json()["id"]

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                status = client.get(f"{self.BASE_URL}/job/{job_id}", headers=headers)
                if status.status_code >= 400:
                    raise ProviderError(f"LlamaParse status failed ({status.status_code})")
                state = status.json().get("status")
                if state == "SUCCESS":
                    break
                if state in {"ERROR", "CANCELED"}:
                    raise ProviderError(f"LlamaParse job {job_id} ended in state {state}")
                time.sleep(self.poll_interval)
            else:
                raise ProviderError(f"LlamaParse job {job_id} timed out after {self.timeout}s")

            result = client.get(f"{self.BASE_URL}/job/{job_id}/result/markdown", headers=headers)
            if result.status_code >= 400:
                raise ProviderError(f"LlamaParse result failed ({result.status_code})")

        payload = result.json()
        pages = payload.get("pages")
        if isinstance(pages, list) and pages:
            return [
                ParsedPage(page=int(p.get("page", i + 1)), text=p.get("md") or p.get("text") or "")
                for i, p in enumerate(pages)
                if (p.get("md") or p.get("text") or "").strip()
            ]
        markdown = payload.get("markdown", "")
        if not markdown.strip():
            raise ProviderError(f"LlamaParse returned no content for {path.name}")
        return [ParsedPage(page=1, text=markdown)]


def get_parser(settings: Settings | None = None) -> Parser:
    settings = settings or get_settings()
    if settings.parser_backend == "local":
        return LocalParser()
    if not settings.llamaparse_api_key:
        raise NotConfiguredError("llamaparse", "set AAD_LLAMAPARSE_API_KEY")
    return LlamaParseParser(api_key=settings.llamaparse_api_key)
