# aad — auto mechanic diagnostic assistant

Grounded diagnostic assistance for professional technicians. Retrieval over OEM service
documentation, plus VIN decoding, trouble-code analysis, torque specs, labor times,
wiring references, parts lookup, and estimate assembly — driven by Claude Opus 5 through
a tool loop.

The organizing constraint is that **the system never originates a specification**. Every
torque value, bolt size, labor hour, wire colour, pin number, part number, and price
either comes from a cited source or is reported as unavailable. That rule is enforced in
code (retrieval scoping, verbatim extraction, explicit gap errors), not only in the
prompt.

## Status

Week 1–2 of the delivery plan (document ingestion) is complete and running, along with
the retrieval, tooling, agent, and API layers it feeds — enough for the pipeline to be
exercised end to end. See [Delivery status](#delivery-status) for what is and is not
built.

## Quick start

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]" pypdf     # pypdf is optional; needed for PDF ingestion

.venv/bin/aad-ingest add data/samples          # index the synthetic sample manual
.venv/bin/aad-ingest stats                     # see what is indexed
.venv/bin/python -m pytest -q                  # 61 tests, no network or API key needed

cp .env.example .env                           # then set ANTHROPIC_API_KEY to use /diagnose
.venv/bin/uvicorn aad.api:app --reload
```

`data/samples/` contains a **synthetic** manual excerpt for testing the pipeline. Its
numbers are invented. Replace it with licensed OEM documentation before the system goes
near a vehicle.

## How grounding is enforced

Four mechanisms, each independently testable:

**Asset-scoped retrieval.** `Retriever.search` raises `UnscopedRequestError` if the
request has no VIN or year/make/model. There is no unscoped search path. Documents are
indexed with vehicle metadata and filtered on it; a document indexed without metadata is
treated as universal (correct for a generic OBD-II reference) and the ingester warns
every time that happens.

**Verbatim extraction.** Torque and labor lookups return the value *and* the sentence it
was extracted from *and* a source/page citation. When candidates disagree, all of them
are returned rather than one being chosen.

**Explicit gaps.** Empty retrieval raises `NoGroundingError`; an unconfigured provider
raises `NotConfiguredError`. Both reach the model as `is_error` tool results carrying
"do not substitute a value from general knowledge", and reach HTTP callers as 404 / 501
with the reason intact.

**No invented commercial data.** Part numbers, prices, and live scan data require a
configured supplier or scan-service account. Unconfigured, those tools say so.

The system prompt is deliberately narrow about what is restricted: published values.
Diagnostic *reasoning* — how a system works, what a symptom implies, what to test next —
is expertise the model should give freely, and it is told so.

## Architecture

```
documents ──► parse ──► chunk (1200 tok / 200 overlap) ──► embed ──► vector index
                          │
                          └─ spec_type classification (torque / labor / wiring / TSB / DTC …)

request ──► DiagnosticAgent (Claude Opus 5)
              ├─ decode_vin ............ NHTSA vPIC + local check-digit validation
              ├─ lookup_dtc ............ SAE generic table + indexed OEM docs
              ├─ obd2_scan ............. configured scan service
              ├─ search_service_info ... asset-scoped RAG
              ├─ lookup_torque_spec .... spec API, else verbatim extraction + citation
              ├─ lookup_labor_time ..... labor API, else verbatim extraction + citation
              ├─ lookup_wiring ......... wiring API, else retrieved text, quoted only
              ├─ search_tsbs ........... indexed bulletins + NHTSA recalls
              ├─ search_parts .......... supplier catalog (required, never inferred)
              └─ build_estimate ........ arithmetic over figures the other tools returned
```

| Module | Responsibility |
|---|---|
| `aad/ingest/` | parsers (local / LlamaParse), chunker, embedders, pipeline, CLI |
| `aad/rag/` | vector store (local numpy / Pinecone), asset-scoped retriever |
| `aad/providers/` | VIN, DTC, OBD2, torque, labor, parts, TSB, wiring |
| `aad/agent/` | tool schemas + dispatch, model client, the tool loop |
| `aad/api/` | FastAPI surface, one endpoint per tool plus `/diagnose` |
| `aad/cache/` | SQLite offline cache with stale-fallback |
| `aad/estimates.py` | estimate arithmetic |

### Model configuration

Claude Opus 5 with adaptive thinking and `effort: high`. The system prompt sits behind a
prompt-cache breakpoint and contains nothing dynamic — per-request vehicle context goes
in the user turn, so the cached prefix survives across requests.

Two deployment targets: the first-party Claude API (default) and Amazon Bedrock
(`AAD_PROVIDER=bedrock`) for VPC-resident deployments, which is what TISAX/SOC 2 work
tends to require. The Bedrock path adds the `anthropic.` model-id prefix and skips
server-side refusal fallbacks, which that platform does not support. On the Claude API,
fallbacks are enabled — automotive work touches immobilizers, key programming, and module
reflashing often enough to trip a safety classifier occasionally, and a fallback recovers
the request instead of dead-ending it.

The agent uses a hand-written tool loop rather than the SDK tool runner, because tools
need per-request session state (`decode_vin` populates the vehicle that later lookups
inherit) and tool results are inspected in flight to harvest citations.

## Defaults that need replacing for production

| Default | Why it is the default | Replace with |
|---|---|---|
| `AAD_EMBEDDING_BACKEND=local` | Deterministic, no key, works offline and in CI | `openai` (`text-embedding-3-large`) for real retrieval quality |
| `AAD_VECTOR_BACKEND=local` | Exact cosine search, no service to run | `pinecone` past a single node |
| `AAD_PARSER_BACKEND=local` | Handles text/markdown/PDF-with-text-layer | `llamaparse` for scanned and heavily-tabular manuals |
| `data/samples/` | Exercises the pipeline | Licensed OEM documentation |

The local embedder is a hashed bag-of-ngrams projection. It is genuinely weaker than a
learned model at paraphrase matching — it is here so the whole system runs with no
account and no network, which the offline-first requirement demands anyway.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/diagnose` | Full agent loop; returns answer, citations, and tool trace |
| `POST /api/v1/vin/decode` | VIN → vehicle, with check-digit validation |
| `POST /api/v1/manuals/search` | Asset-scoped RAG over indexed documentation |
| `POST /api/v1/torque-specs` | Torque values with verbatim source text |
| `POST /api/v1/labor-times` | Published labor hours with citations |
| `POST /api/v1/wiring-diagrams` | Wiring/pinout text, quoted verbatim |
| `POST /api/v1/dtc/lookup` | Trouble-code structure and definition |
| `POST /api/v1/obd2/scan` | Stored codes and live data from the scan service |
| `POST /api/v1/tsb/search` | Bulletins plus NHTSA recalls |
| `POST /api/v1/parts/search` | Supplier catalog |
| `POST /api/v1/estimates` | Estimate assembly from supplied figures |
| `GET /api/v1/index/stats` | What is indexed, and cache contents |

Status codes carry meaning: **422** the request had no vehicle identity, **404** nothing
indexed matches, **501** the provider is not configured, **502** the upstream provider
failed.

```bash
curl -s localhost:8000/api/v1/torque-specs -H 'content-type: application/json' -d '{
  "component": "camshaft position sensor retaining bolt",
  "vehicle": {"year": 2004, "make": "INFINITI", "model": "G35", "engine": "3.5L V6 VQ35DE"}
}'
```

## Ingestion

```bash
aad-ingest add /path/to/manuals --year 2004 --make INFINITI --model G35 --engine "3.5L V6 VQ35DE"
aad-ingest add /path/to/manuals          # or use <file>.meta.json sidecars per document
aad-ingest stats
aad-ingest remove sample_service_manual.md
```

Re-ingesting a document replaces its chunks rather than duplicating them. A per-file
sidecar is the better path for a mixed directory:

```json
{"year": 2004, "make": "INFINITI", "model": "G35", "engine": "3.5L V6 VQ35DE"}
```

## Tests

61 tests, no network and no API key required. The agent loop is tested against a scripted
client, so tool dispatch, citation harvesting, refusal handling, and the turn budget are
all covered without calling the API.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
```

## Delivery status

**Built and tested.** Ingestion pipeline (parse/chunk/classify/embed/index, local and
LlamaParse, local and Pinecone) · asset-scoped retrieval with grounding enforcement ·
VIN decode and check-digit validation · DTC structural decode and definition lookup ·
torque and labor extraction with citations · wiring lookup · TSB/recall search · parts
and OBD2 provider adapters · estimate assembly · Claude Opus 5 agent with 10 tools ·
FastAPI surface · SQLite offline cache.

**Not built.** The React Native client (weeks 7–10), shop-management integrations
(Tekmetric/AutoLeap), on-device cache sync, and the beta/production deployment steps
(weeks 15–18). The offline cache exists server-side; the client half of sync does not.

**Blocked on credentials, not code.** Mitchell 1, ALLDATA, NAPA, and the OBD2 scan
service have no public APIs. Each has an adapter with a configurable base URL and key,
and each returns `NotConfiguredError` until the shop's own subscription is wired in. The
adapters assume a conventional REST shape and will need their request/response mapping
adjusted to each vendor's actual contract — a small change per provider, isolated to one
function.

**Verified vs unverified.** Everything above is covered by the test suite. The live NHTSA
calls (vPIC decode, recalls) are unit-tested for validation and parsing logic but could
not be exercised against the real service from this sandbox — outbound requests to
`vpic.nhtsa.dot.gov` are blocked by the environment's proxy. Run that path once on a
networked machine before relying on it.

## One deviation from the specification

The architecture doc's agent snippet used LangChain's `create_openai_tools_agent`, which
is the OpenAI-flavored path, while the rest of the doc specifies Claude Opus 5 on
Bedrock. The agent here is built on the official Anthropic SDK against the same tool set,
and supports both deployment targets. A LangChain wrapper can be added over
`aad.agent.tools` if that orchestration layer is needed elsewhere.

## Safety

This system supports a technician's judgment; it does not replace it. It performs no
physical work and authorizes none. Torque-to-yield fasteners, single-use hardware, and
brake and suspension work are flagged, but the technician still verifies against the OEM
source before the wrench moves.
