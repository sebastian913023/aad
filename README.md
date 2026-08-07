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

.venv/bin/aad ingest add data/samples    # index the synthetic sample manual
.venv/bin/aad check                      # lint + 73 tests, no network or API key needed
.venv/bin/aad verify-index               # what is indexed, and whether it is fit to serve
.venv/bin/aad doctor                     # live-check NHTSA and report provider config

cp .env.example .env                     # then set ANTHROPIC_API_KEY to use /diagnose
.venv/bin/aad serve --reload
```

`data/samples/` contains a **synthetic** manual excerpt for testing the pipeline. Its
numbers are invented, it is flagged `synthetic: true`, and the guards below make sure it
cannot reach a technician. Replace it with licensed OEM documentation before the system
goes near a vehicle.

## The `aad` command

| Command | Purpose |
|---|---|
| `aad ingest add\|stats\|remove` | index service documentation |
| `aad serve` | run the HTTP API |
| `aad check [--live]` | run exactly what CI runs: ruff, then pytest |
| `aad doctor` | live-check NHTSA reachability and report which providers are configured |
| `aad verify-index [--production]` | report whether the indexed corpus is fit to serve |
| `aad providers list\|show\|test` | inspect and live-test the commercial provider profiles |
| `aad eval --goldset PATH` | score accuracy, precision and hallucination rate |

`aad check` exists so a red PR is reproducible locally with one command. `doctor` and
`verify-index` answer a different question from the test suite: tests tell you the code
is correct, these tell you whether the *data* behind it is real, licensed and
vehicle-scoped — which no unit test can determine.

## Configuring the commercial providers

Mitchell 1, ALLDATA, NAPA and the OBD2 scan services are partner-gated — no public API
contract, and each differs in auth scheme, path, parameter names and response envelope.
Rather than one guessed REST shape hard-coded per provider, each is described by a JSON
**profile** in `src/aad/providers/profiles/`. Pointing the system at a real subscription
is a config change, not a code change.

```bash
export AAD_TORQUE_API_BASE=https://your-prodemand-endpoint/api
export AAD_TORQUE_API_KEY=...              # or put both in .env

aad providers list                          # configuration state for all five
aad providers show torque                   # the exact request it would send
aad providers test torque --arg "cylinder head bolts" --year 2004 --make INFINITI --model G35
```

`aad providers test` makes one real call and prints the mapped result beside the fields
the mapping produced nothing for, so a field-name mismatch is visible immediately rather
than surfacing later as a torque value with no unit.

### Adjusting a profile to your vendor

Copy the shipped profile, edit it to match the vendor's own API documentation, and point
`AAD_PROVIDER_PROFILE_DIR` at your directory:

```json
{
  "name": "torque",
  "vendor": "Mitchell 1 ProDemand",
  "verified": true,
  "base_url_env": "AAD_TORQUE_API_BASE",
  "credential_env": "AAD_TORQUE_API_KEY",
  "auth": { "type": "header", "name": "X-Api-Key" },
  "method": "GET",
  "path": "/v2/specifications/torque",
  "params": { "vehicleYear": "year", "vehicleMake": "make", "part": "component" },
  "results_path": "data.specifications",
  "field_map": { "component": "partName", "value": "torque.value", "unit": "torque.units" }
}
```

`auth.type` is one of `bearer`, `header`, `query`, `basic`, `none`. `params` maps the
vendor's wire parameter name to our field (`year`, `make`, `model`, `engine`, `vin`, plus
the endpoint's own argument). `results_path` and `field_map` take dotted paths into the
response, list indices included.

### `verified: false`

Every shipped profile carries `"verified": false`, and a test asserts it stays that way.
That flag means precisely what it says: **the request and response shapes are placeholders
derived from vendor product documentation, not a contract anyone has exercised.** They
will almost certainly need adjusting against your account's real API docs. Flip the flag
to `true` only after `aad providers test` returns correct data — `aad providers list` and
`aad doctor` both report configured-but-unverified providers as a warning, and every
lookup response carries `provider_verified` so the state travels with the data.

Credentials are read from the environment (or `.env`, which is gitignored) and never
written to the repo. They are redacted from provider error messages, because those
messages reach both the model and HTTP callers.

## Accuracy evaluation

The headline number for this system is not accuracy — it is the **hallucination rate**.
A system that abstains on half its questions and is never wrong is usable in a bay; one
that answers everything and is wrong 3% of the time is not, because the technician cannot
tell which 3%.

So every gold case scores into one of four outcomes:

| Outcome | Meaning |
|---|---|
| `correct` | answered, and the value matches the gold value |
| `wrong` | answered, and it does not — **the gate metric** |
| `abstained` | correctly reported the data as unavailable |
| `missed` | abstained when the answer was in the corpus (safe, unhelpful) |

```bash
aad eval --goldset path/to/goldset.jsonl
```

A gold case is a question with a known answer drawn from your licensed corpus. Cases with
`"must_abstain": true` are the important ones — they name a vehicle or component the
corpus does *not* cover, so any returned specification is a hallucination:

```json
{"id": "g35-cmp-bolt", "kind": "torque_spec",
 "vehicle": {"year": 2004, "make": "INFINITI", "model": "G35", "engine": "3.5L V6"},
 "query": "camshaft position sensor retaining bolt",
 "expected_value": 9, "expected_unit": "Nm", "expected_source": "nissan_g35_engine.pdf"}
{"id": "g35-absent", "kind": "torque_spec",
 "vehicle": {"year": 2004, "make": "INFINITI", "model": "G35"},
 "query": "transfer case output shaft nut", "must_abstain": true}
```

`kind` is `torque_spec`, `labor_time` or `retrieval`. Gold values may be given in any
torque unit — comparison converts, though nothing the technician sees is ever silently
converted. Gates default to **zero tolerated hallucinations** and **100% citation
coverage**; `--min-accuracy` is available but off by default, because a miss is a
different kind of problem from a wrong answer.

**Evaluation refuses to run over synthetic material.** An accuracy figure measured
against invented specifications reads as evidence while meaning nothing. `--allow-synthetic`
exercises the harness itself on the sample corpus and labels the output as not an accuracy
result.

### What the harness caught on its first run

A torque query for a component the corpus does not cover — *transfer case output shaft
nut* on a vehicle that **is** indexed — returned **40 Nm, the cylinder head bolt value**,
with a citation attached. Retrieval returns the closest chunks for the vehicle whether or
not the component appears in them, and extraction pulled any torque value out of them,
ranked by relevance but never gated on it.

Extraction is now gated: a value is only offered as a component's spec when the sentence,
or the section heading above it, actually names that component. Two follow-on bugs fell
out of fixing it — substring matching equated `shaft` with `camshaft`, and splitting
sentences on `:` stranded values like `bank 1: 0.7 hrs` in a fragment naming nothing.
`tests/test_evaluation.py` keeps all three caught.

## Production readiness

Passing tests does not mean this is safe to put in front of a technician. Two gates
separate the two:

**Synthetic material is flagged and fenced.** A document whose sidecar carries
`"synthetic": true` marks every chunk it produces. With `AAD_PRODUCTION_MODE=true` the
retriever excludes those chunks from every search, so sample data physically cannot
reach a bay even if someone indexes it by accident.

**`aad verify-index --production` is the gate.** It exits non-zero if the index is
empty, contains synthetic chunks, or contains chunks without year/make/model. CI asserts
this gate *fails* on the bundled sample corpus — if it ever passes there, the guard has
regressed and the build goes red.

```
$ aad verify-index --production
chunks:           6
synthetic:        6 across 1 source(s)

NOT READY FOR PRODUCTION USE
  - 6 synthetic chunk(s) from: sample_service_manual.md. These carry invented values
    and must not back a production deployment.
```

The gate checks provenance flags, not licensing. It cannot tell you whether a document
is covered by your subscription — that remains a human decision.

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

**Output verification.** Every finished answer is re-checked against the documents it
actually retrieved, before it reaches a technician. See the next section.

The system prompt is deliberately narrow about what is restricted: published values.
Diagnostic *reasoning* — how a system works, what a symptom implies, what to test next —
is expertise the model should give freely, and it is told so.

## Grounding monitor (zero tolerance)

Everything above constrains what the model is *given*. The monitor checks what it
*produced*. It runs on every answer from `/api/v1/diagnose`, and can be pointed at any
other text via `aad monitor verify` or `POST /api/v1/monitor/verify`.

### How a claim is judged

The output is decomposed into **atomic claims** — torque values, labor hours, part
numbers, wire colours, pin numbers, bolt sizes — and each is scored independently
against the sources that answer cited. Scoring whole responses cannot enforce zero
tolerance: one fabricated value inside four correct paragraphs averages out to a good
score.

Three signals per claim, reported separately (**LSC**):

| | | |
|---|---|---|
| **L** | lexical grounding | the claim's literal value appears in a cited source |
| **S** | semantic consistency | the claim and its best-matching source passage agree |
| **C** | citation validity | the citation names a source that was actually supplied |

`LSC` is this system's own composite, defined in `src/aad/monitor/lsc.py`. It is not a
published standard metric. The composite (`0.5L + 0.3S + 0.2C`) is reported for trend
watching only — **the gate is L**, and L is binary. A torque value that does not appear
verbatim in a cited source is a fabrication however plausible the sentence around it
reads. Unit spellings are normalised (`N·m` grounds `Nm`) but values are never
converted: 9 Nm does not ground a claim of "6.6 lb-ft", because a converted number is
not a published number.

### The order of operations

1. Extract claims.
2. Score each against its cited sources (deterministic, always runs, no model call).
3. No sources reached the monitor at all → **abstain**. An unverifiable claim is not a
   proven fabrication, and our own plumbing failures must not land in the hallucination
   rate.
4. Any claim with `L == 0` → **blocked**. The answer is withheld and replaced by a
   notice naming the values that could not be verified; the original text is kept in
   `unverified_answer` for the reviewer.
5. Judge model (optional, `use_judge`) — a second Claude call that reads the output and
   the sources. **It can only tighten the outcome.** It may raise an abstention; it can
   never clear a fabrication the lexical check already found. A soft judge able to
   overturn a hard lexical failure would turn a deterministic guarantee back into a
   probabilistic one. A judge that fails to run returns `supported=False,
   insufficient_context=True`, so an unavailable judge routes to review rather than
   reading as approval.
6. Route: `grounded` / `abstained` / `escalated` / `blocked` / `no_claims`.

### Abstention triggers

An output goes to human review when any of these fire:

- **insufficient_context** — no sources supplied, or the judge says the sources do not
  contain enough to answer.
- **rubric_conflict** — the output asserts a value *and* declares it unavailable, or the
  judge finds cited sources disagreeing with each other.
- **low_semantic_consistency** — mean S below `AAD_MONITOR_MIN_SEMANTIC`.
- **fabrication** — the zero-tolerance block.

Escalation (answer kept, review flagged) additionally fires when citation accuracy is
below 1.0, or when the judge cannot confirm support for every claim.

The default semantic floor (0.15) is calibrated for the offline hash embedder, which
measures lexical overlap and scores a terse sentence low however well the source
supports it. Raise it toward ~0.5 when `AAD_EMBEDDING_BACKEND=openai`. It is not the
fabrication gate — L is.

### Dashboard

```bash
.venv/bin/aad monitor summary        # headline metrics; exits 1 if any hallucination recorded
.venv/bin/aad monitor dashboard      # self-contained HTML, no server and no network
.venv/bin/aad monitor queue          # outputs awaiting human review
.venv/bin/aad monitor resolve <id> --reviewer you --outcome confirmed|corrected|rejected
.venv/bin/aad serve                  # then open /monitor
```

The dashboard shows faithfulness, citation accuracy and semantic consistency; the
verdict mix; abstention-versus-grounding rates broken out **by task type** (torque_spec,
labor_time, wiring, dtc, tsb, parts, estimate, obd2_scan, manual_search, vin_decode);
which abstention trigger fired how often; daily activity; and every fabrication caught,
with the offending value and the sentence it appeared in.

Rates over an empty denominator render as `—`, never `0%`. A 0% hallucination rate over
zero samples is not a safety claim, and showing it as one is how people come to trust an
untested system. The hallucination rate's denominator is claim-bearing outputs only —
counting answers that asserted nothing would dilute the rate toward zero exactly when
the system starts answering less.

Every verification is logged to SQLite (`AAD_MONITOR_DB`, default
`data/monitor.sqlite3`), one row per answer, with the per-claim evidence. That log is
the audit trail: for any answer a technician acted on, it shows what was claimed, what
was cited, whether the value was found, and who signed it off. Resolution never rewrites
the original verdict.

Set `AAD_MONITOR_ENABLED=false` to switch the monitor off. There is no good reason to do
that in a bay.

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
| `aad/monitor/` | zero-tolerance grounding monitor: claim extraction, LSC scoring, judge, pipeline, SQLite event store, dashboard |

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
| `POST /api/v1/monitor/verify` | Grounding-check an arbitrary output against supplied sources |
| `GET /api/v1/monitor/summary` | Hallucination/grounding/abstention rates, overall and by task type |
| `GET /api/v1/monitor/events` | Recent monitor events (filterable by task type, verdict) |
| `GET /api/v1/monitor/review-queue` | Outputs currently awaiting human review |
| `POST /api/v1/monitor/events/{id}/review` | Close out a human review |
| `GET /monitor` | The dashboard, rendered as HTML |

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

## Tests and CI

164 hermetic tests, no network and no API key required. The agent loop is tested against a
scripted client, so tool dispatch, citation harvesting, refusal handling, and the turn
budget are all covered without calling the API. The monitor is tested the same way — the
load-bearing case plants a plausible, well-cited, confidently-worded torque value that is
simply not in the source, and asserts it gets blocked.

```bash
.venv/bin/aad check           # ruff + pytest, the same gates CI runs
.venv/bin/aad check --live    # also run the live provider tests
```

CI (`.github/workflows/ci.yml`) runs on every push and PR, as two jobs:

- **tests + lint** — ruff, pytest, an ingestion smoke test, a step that plants a
  fabricated torque value and asserts `aad monitor verify` exits non-zero on it (and zero
  on the grounded version), and the readiness-gate assertion described above.
- **live provider APIs (NHTSA)** — `aad doctor` plus 8 tests that hit the real vPIC and
  recalls services. Kept in its own job on purpose: an NHTSA outage should read as an
  upstream problem, not as a failure of the diff under review. It also runs weekly on a
  schedule so upstream drift surfaces without waiting for a push.

Live tests are marked `live` and deselected by default (`-m 'not live'` in
`pyproject.toml`), additionally gated on `AAD_LIVE_TESTS=1`. They assert on response
*shape and semantics* rather than specific vehicle data, because the vPIC dataset changes
over time and a test pinned to today's trim string would fail for reasons unrelated to
this code.

## Delivery status

**Built and tested.** Ingestion pipeline (parse/chunk/classify/embed/index, local and
LlamaParse, local and Pinecone) · asset-scoped retrieval with grounding enforcement ·
VIN decode and check-digit validation · DTC structural decode and definition lookup ·
torque and labor extraction with citations · wiring lookup · TSB/recall search · parts
and OBD2 provider adapters · estimate assembly · Claude Opus 5 agent with 10 tools ·
FastAPI surface · SQLite offline cache · zero-tolerance grounding monitor (claim
extraction, LSC scoring, judge model with tighten-only override, SQLite audit log,
review queue, HTML dashboard) wired into every agent answer.

**Not built.** The React Native client (weeks 7–10), shop-management integrations
(Tekmetric/AutoLeap), on-device cache sync, and the beta/production deployment steps
(weeks 15–18). The offline cache exists server-side; the client half of sync does not.

**Blocked on credentials, not code.** Mitchell 1, ALLDATA, NAPA, and the OBD2 scan
service have no public APIs. Each has an adapter with a configurable base URL and key,
and each returns `NotConfiguredError` until the shop's own subscription is wired in. The
adapters assume a conventional REST shape and will need their request/response mapping
adjusted to each vendor's actual contract — a small change per provider, isolated to one
function.

**Verified vs unverified.** Everything above is covered by the hermetic suite. The live
NHTSA path (vPIC decode, recalls) has tests written against the real services, but they
have never been executed in the development sandbox: outbound CONNECT to
`vpic.nhtsa.dot.gov:443` and `api.nhtsa.gov:443` is refused with 403 by the environment's
egress policy. **CI runs them** — the `live provider APIs` job executes on a GitHub
runner with open internet, so the PR's check status is the authoritative answer on
whether the NHTSA integration works. Until that job has gone green at least once, treat
the NHTSA path as untested.

## Not ready to touch vehicles

Being blunt about this, because the failure mode is expensive:

1. **The corpus is synthetic.** No licensed OEM documentation is included, and none can
   be — Mitchell 1, ALLDATA and OEM manuals are proprietary and paywalled. The invented
   sample values must never be treated as specifications, which is what the
   `synthetic` flag and the readiness gate exist to enforce.
2. **Every commercial provider is unconfigured.** Torque, labor, parts, wiring and OBD2
   lookups all report a gap until a shop wires in its own subscription, and each adapter's
   request/response mapping will need adjusting to that vendor's actual contract.
3. **The live NHTSA integration is unverified** until CI's live job passes.
4. **No accuracy evaluation has been run against real manuals.** The harness exists
   (`aad eval`) and has already earned its keep by catching a live hallucination path,
   but it can only self-test on synthetic data. A real accuracy figure needs (1)
   resolved first, and the harness refuses to produce one before then.

The system is a technician's reference, not an authority. Nothing here removes the
technician's obligation to verify a specification against the OEM source before the
wrench moves.

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
