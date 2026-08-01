# LocZ Business Data Engine — System Design (v0.1, for approval)

Standalone project. Does **not** live in, import from, or write to the LocZ application.
Output contract with LocZ is files + an authenticated read-only export API.

Target host: this Windows 11 machine. Runs as a background Windows Scheduled Task,
auto-starting at boot, active whenever the machine is on.

---

## 0. Environment decisions (confirmed)

| Concern | Decision |
|---|---|
| Database | PostgreSQL **18** installed natively, side-by-side with existing PG 16, on port **5433**. PostGIS 3.x via StackBuilder. DB `locz_engine`. |
| Queue/broker | **Memurai** (native Redis for Windows) on 6379 + **Celery** workers + Celery beat. |
| Containers | None. Docker Compose file still shipped for future Linux VPS deploy, but local run is native. |
| Python | 3.12 (already installed), venv at `backend/.venv`. |
| Node | 24 LTS (already installed), for the Next.js admin. |
| Process supervision | Windows Scheduled Tasks (§13). |

---

## 1. System architecture

```
                        ┌────────────────────────────────────────────┐
                        │            admin-web (Next.js)             │
                        │  dashboard · sources · imports · review    │
                        │  merge · exports · field survey form       │
                        └───────────────┬────────────────────────────┘
                                        │ HTTPS + session cookie → JWT
                        ┌───────────────▼────────────────────────────┐
                        │              api (FastAPI)                 │
                        │  authn/authz · RBAC · OpenAPI · validation │
                        └───┬───────────────┬───────────────┬────────┘
                            │               │               │
             enqueue        │        read/write             │ read-only
                            │               │               │
    ┌───────────────────────▼──┐   ┌────────▼─────────┐  ┌──▼────────────────┐
    │  Redis (Memurai) broker  │   │  PostgreSQL 18   │  │  export artifacts │
    │  + result backend        │   │  + PostGIS       │  │  (local filesystem│
    └───────────┬──────────────┘   └────────▲─────────┘  │   ./var/exports)  │
                │                           │            └───────────────────┘
    ┌───────────▼───────────────────────────┴──────────────────────────────┐
    │                      celery workers  (queues below)                  │
    │  fetch │ parse │ normalise │ geo │ dedupe │ quality │ export │ maint  │
    └───────────┬──────────────────────────────────────────────────────────┘
                │  only via ComplianceGate
    ┌───────────▼──────────────────────────────────────────────────────────┐
    │  source adapters:  osm_overpass │ osm_pbf │ gov_api │ gov_file │     │
    │                    partner_file │ field_survey │ owner_submission │  │
    │                    user_submission                                   │
    └──────────────────────────────────────────────────────────────────────┘
```

**Hard rule enforced in code:** every outbound network fetch goes through
`ComplianceGate.acquire(source)` → returns a throttled HTTPX client or raises
`SourceNotPermitted`. There is no other HTTP client available to adapters
(enforced by an import-linter contract in CI).

### Data flow

```
source → raw_records (immutable payload + checksum)
       → normalise → staged_records
       → geo validate/enrich
       → dedupe (block → score → decide)
       → business (canonical)  +  business_field_sources (field provenance)
       → quality scoring → publication eligibility
       → review queue (human) → approved
       → export run → manifest + checksum → LocZ consumes
```

Raw payloads are never mutated. Normalisation is re-runnable from raw at any time,
which makes the whole pipeline replayable and auditable.

---

## 2. Repository structure

```
locz-business-data-engine/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory
│   │   ├── config.py               # pydantic-settings, fail-fast env validation
│   │   ├── db.py                   # engine, session, PostGIS types
│   │   ├── api/
│   │   │   ├── deps.py             # auth deps, role guards, pagination
│   │   │   └── v1/                 # sources, imports, businesses, duplicates,
│   │   │                           # exports, claims, deletion_requests,
│   │   │                           # surveys, taxonomy, health
│   │   ├── auth/                   # password hashing, JWT, refresh rotation, RBAC
│   │   ├── compliance/
│   │   │   ├── gate.py             # ComplianceGate — the only egress path
│   │   │   ├── denylist.py         # hard-coded prohibited domains (§2 of brief)
│   │   │   ├── robots.py           # robots.txt fetch/parse/cache
│   │   │   ├── ratelimit.py        # token bucket in Redis, per source
│   │   │   ├── licences.py         # licence model + export-time enforcement
│   │   │   └── retention.py        # raw payload TTL sweeper
│   │   ├── sources/
│   │   │   ├── base/               # SourceAdapter ABC, registry, capabilities
│   │   │   ├── osm/                # overpass.py, pbf.py, tags.py
│   │   │   ├── government/         # api.py, file.py
│   │   │   ├── partner_files/
│   │   │   ├── field_surveys/
│   │   │   └── submissions/        # owner + user
│   │   ├── ingestion/              # batches, jobs, checkpoints, dry-run, modes
│   │   ├── normalisation/          # name, phone, email, url, address, hours,
│   │   │                           # status, category_map
│   │   ├── geospatial/             # validate, accuracy, regions, geocode adapters
│   │   ├── deduplication/          # blocking, scoring, decide, merge, unmerge
│   │   ├── quality/                # completeness, freshness, confidence, eligibility
│   │   ├── moderation/             # review queue, actions, state machine
│   │   ├── provenance/             # field-source writes, conflict resolution
│   │   ├── exports/                # builders (csv/json/jsonl/geojson), manifest
│   │   ├── privacy/                # suppression, deletion, obfuscation
│   │   ├── models/                 # SQLAlchemy 2 declarative
│   │   ├── schemas/                # pydantic v2
│   │   ├── workers/                # celery app, beat schedule, task modules
│   │   └── observability/          # structlog, correlation ids, metrics, health
│   ├── migrations/                 # alembic
│   ├── tests/  {unit,integration,compliance,e2e}/
│   └── pyproject.toml
├── admin-web/                      # Next.js 15 App Router + TS + Tailwind + shadcn/ui
├── infrastructure/
│   ├── windows/                    # install-tasks.ps1, uninstall, run-worker.ps1,
│   │                               # run-beat.ps1, run-api.ps1, healthcheck.ps1
│   ├── docker/                     # for later Linux VPS deploy
│   ├── nginx/  monitoring/  backup/
├── sample-data/
├── export-schemas/                 # locz-export-v1.json (JSON Schema) + fixtures
├── docs/
├── .env.example
└── README.md
```

---

## 3. Database schema

PostgreSQL 18 + PostGIS. All tables have `id uuid pk default gen_random_uuid()`,
`created_at`, `updated_at`. Soft-delete via `deleted_at` where recovery matters.

### 3.1 Compliance & sources

**`data_sources`** — exactly the field list in §5 of the brief, plus:
`slug` (unique), `adapter_key` (FK to registered adapter), `config jsonb`
(adapter-specific: bbox, overpass endpoint, dataset url, column map),
`secret_ref` (name of env var holding credentials — never the credential itself),
`last_success_at`, `last_error_at`, `consecutive_failures`, `circuit_open_until`.

`compliance_status` enum: `pending_review · approved · approved_with_restrictions ·
blocked · expired · suspended`. Default `pending_review`.
Partial index: adapters may only load sources where
`enabled AND compliance_status IN ('approved','approved_with_restrictions')`.

**`source_compliance_events`** — append-only: who changed status, from→to, reason,
evidence urls, timestamp. Never updated, never deleted.

**`source_licences`** — `name, spdx_id, url, commercial_use, redistribution,
modification, share_alike, attribution_required, attribution_template, notes`.
Sources reference a licence; exports aggregate licences from contributing sources.

**`robots_cache`** — `host, fetched_at, expires_at, body, parsed jsonb`.

### 3.2 Raw ingestion

- **`import_batches`** — source_id, mode (`dry_run|incremental|full_refresh|file|api`),
  requested_by, filters jsonb (city/bbox/categories), status, counters, started/finished.
- **`import_jobs`** — batch_id, adapter_key, celery task id, state
  (`queued·running·paused·cancelled·failed·succeeded`), checkpoint jsonb (cursor/page/
  offset for resume), attempts, last_error.
- **`raw_files`** — batch_id, original filename, stored path, mime, size, sha256,
  virus_scan_status, uploaded_by.
- **`raw_records`** — batch_id, source_id, source_record_id, payload jsonb,
  payload_sha256 (unique with source_id → natural idempotency), source_timestamp,
  ingested_at, parse_status, normalisation_status, error jsonb,
  **`purge_after`** (set from `data_retention_days`; sweeper nulls `payload` and
  marks `payload_purged_at` when reached).
- **`import_errors`**, **`import_statistics`**.

### 3.3 Canonical business

**`businesses`** — the full §7 field list. Notable column types:

- `geom geometry(Point, 4326)` + GiST index; `latitude/longitude` kept as
  generated columns for export convenience.
- `location_accuracy` enum: `exact_storefront · building · street · locality ·
  pincode_centroid · city_centroid · unknown`.
- `opening_hours jsonb` — normalised structured form + `opening_hours_raw text`.
- `social_links jsonb`, `service_areas geometry(MultiPolygon,4326)` nullable.
- `lifecycle_status` enum: `imported · normalised · needs_review ·
  duplicate_candidate · approved · rejected · quarantined · ready_for_export ·
  exported · outdated · closure_reported · permanently_closed ·
  deletion_requested · suppressed`.
- `claim_status`: `unclaimed · claim_pending · claimed · ownership_disputed`.
- `verification_status`: `unverified · source_verified · contact_verified ·
  owner_verified · manually_verified`. **Import can never set anything above
  `source_verified`** — enforced by a DB check constraint tied to the origin.
- Scores: `confidence_score, completeness_score, freshness_score` smallint 0–100.
- Privacy: `do_not_publish bool`, `suppressed_reason`, `location_obfuscation`
  enum (`none · locality_only · service_area · approximate_point`).
- LocZ: `locz_export_status, locz_exported_at, locz_external_id (stable, minted
  once, never reused), locz_import_version, locz_rejection_reason`.

Indexes: GiST on `geom`; trigram GIN on `canonical_name`; btree on
`(city, lifecycle_status)`, `(pincode)`, `(normalised_phone)`, `(website_domain)`.

**`business_field_sources`** — §8 verbatim, plus `precedence_rank` (computed from
the conflict-resolution rules) and unique partial index
`(business_id, field_name) WHERE NOT superseded AND approved` so exactly one value
per field is "current". Superseding is an insert + flag flip, never a delete.

**`business_source_links`** — business_id ↔ source_id ↔ source_record_id, so one
business can be traced to every contributing raw record.

### 3.4 Taxonomy

- **`categories`** — canonical tree (the §10 list seeded), `parent_id`, `slug`,
  `level`, `active`.
- **`source_category_mappings`** — source_id, source_category_key,
  source_category_label, category_id, confidence, mapping_type
  (`auto·manual·override`), created_by. **No mappings in adapter code.**
- **`unmapped_source_categories`** — queue with occurrence counts, feeding an
  admin screen.

### 3.5 Dedup, review, exports, privacy

- **`duplicate_candidates`** — business_a, business_b (ordered pair, unique),
  score, matched_fields jsonb, conflicting_fields jsonb, outcome enum
  (`exact_match·probable_match·possible_match·no_match·conflict·manual_review`),
  recommendation, decided_by, decided_at.
- **`merges`** — surviving_id, merged_id, strategy, field_choices jsonb,
  **`snapshot jsonb` (full pre-merge state of both records)** → enables `unmerge`.
- **`review_actions`** — business_id, actor, action, before jsonb, after jsonb, note.
- **`exports`** — filters jsonb, schema_version, format, record_count,
  source_summary jsonb, licence_summary jsonb, attribution_block text,
  file_path, sha256, created_by, status.
- **`export_records`** — export_id ↔ business_id ↔ locz_external_id (what went out,
  when — so a later export can diff).
- **`deletion_requests`**, **`claims`**, **`closure_reports`**, **`consents`**
  (subject, method, evidence_path, captured_by, captured_at, withdrawn_at).
- **`images`** — §22 field list; `download_status` defaults to `metadata_only`.
- **`audit_log`** — actor, role, action, entity_type, entity_id, before/after,
  ip, correlation_id, at. Append-only; write via trigger where feasible.
- **`users`**, **`roles`**, **`refresh_tokens`** (rotating, revocable, hashed).

---

## 4. Source-adapter interface

```python
class SourceAdapter(ABC):
    key: ClassVar[str]                       # "osm_overpass"
    capabilities: ClassVar[Capabilities]     # dry_run, incremental, resume,
                                             # bbox, category_filter, needs_network
    config_schema: ClassVar[type[BaseModel]] # validates data_sources.config

    def validate_config(self, cfg) -> None: ...
    def test_connection(self, ctx) -> ConnectionReport: ...

    @abstractmethod
    def discover(self, ctx: JobContext) -> Iterator[WorkUnit]:
        """Split the job into resumable units (tile, page, file chunk)."""

    @abstractmethod
    def fetch(self, ctx: JobContext, unit: WorkUnit) -> Iterator[RawRecord]:
        """Yield raw payloads. MUST use ctx.client (the gated client)."""

    @abstractmethod
    def to_staged(self, raw: RawRecord) -> StagedRecord:
        """Source-shape → canonical-ish, with per-field FieldValue(value,
        source_field, observed_at, confidence). No DB access, pure, unit-testable."""

    def checkpoint(self, unit: WorkUnit) -> dict: ...
```

`JobContext` carries: source row, licence, rate limiter, gated HTTPX client,
cancellation token, checkpoint store, logger with correlation id, dry-run flag.

Registration is via entry-point-style decorator; `adapter_key` on a source must
resolve to a registered adapter or the source cannot be enabled.

**Egress guarantee:** `ctx.client` is the only HTTPX client instance reachable from
`app.sources.*`. An import-linter contract forbids `httpx`, `requests`, `urllib`,
`playwright`, `selenium` imports anywhere under `app/sources/` and `app/ingestion/`.
CI fails on violation. This is what makes "no proprietary scraping" structural
rather than aspirational.

---

## 5. Compliance model

`ComplianceGate.acquire(source)` runs, in order — any failure raises and is logged
to `source_compliance_events`:

1. `source.enabled` is true.
2. `compliance_status ∈ {approved, approved_with_restrictions}`.
3. Registered domain is **not** in the hard denylist (google.*, maps.google.*,
   business.google.com, justdial, indiamart, facebook, instagram, linkedin,
   sulekha, zomato, swiggy, magicbricks, 99acres, housing.com, …). The denylist is
   code, not data — no admin can whitelist these through the UI.
4. `automated_access_allowed` is true (skipped for file/manual adapters).
5. robots.txt fetched, cached, and permits the target path for our UA;
   `Crawl-delay` honoured if longer than configured.
6. Licence permits `storage_allowed`; if the job's purpose is export,
   `redistribution_allowed` must also hold.
7. Rate limit token available (per-minute and per-day buckets in Redis).
8. Circuit breaker closed.
9. Retention policy resolvable (`data_retention_days` set or licence unrestricted).

Unknown source → `pending_review` → gate fails. **Default deny.**

Export time re-checks: a record whose contributing source has since become
`blocked`/`expired` is excluded and reported in the export manifest as suppressed.

No CAPTCHA solving, proxy rotation, fingerprint spoofing, cookie reuse, or auth
bypass exists anywhere in the codebase, and there is no configuration surface that
could enable it.

---

## 6. Canonical business schema

As §7 of the brief, realised in §3.3 above. Principles applied:

- Every canonical field is backed by `business_field_sources` rows — the canonical
  column is a **materialised winner**, recomputable from provenance.
- Original source values are preserved on the provenance row (`source_value`)
  alongside `normalised_value`. Normalisation never destroys.
- Nothing is inferred. A missing phone stays null; a category never implies
  products; no synthesised hours, ratings, offers, or prices exist in the schema at
  all (there are no ratings/reviews/products tables — by design).

---

## 7. Deduplication approach

**Stage 1 — blocking** (cheap, indexed, generates candidate pairs only):
- same normalised phone (E.164)
- same website registered domain
- same source + source_record_id
- trigram name similarity ≥ 0.45 **within 750 m** (PostGIS `ST_DWithin`)
- same pincode + trigram name similarity ≥ 0.60

**Stage 2 — scoring** (weighted, produces 0–1 + explanation):

| Signal | Weight | Notes |
|---|---|---|
| Phone exact (valid mobile/landline, not shared) | .30 | shared-number penalty if the number maps to >3 businesses |
| Website domain exact | .20 | ignores generic hosts (blogspot, wix subdomain roots) |
| Name similarity (Jaro-Winkler + token-set, brand-aware) | .20 | |
| Distance | .15 | 1.0 @ ≤50 m, decaying to 0 @ 1 km |
| Address similarity (house/street/locality tokens) | .10 | |
| Category agreement | .05 | penalty on hard conflict (clinic vs restaurant) |

**Stage 3 — decision:**
- auto-merge only on: same source_record_id; **or** valid phone match + ≤300 m;
  **or** domain match + ≤300 m; **or** score ≥ .92 with ≥2 independent strong
  signals and no hard conflict.
- `.70–.92` → `probable_match` → human review queue.
- `.45–.70` → `possible_match` → low-priority queue.
- Name-only similarity **never** auto-merges, at any score. ("Sri Balaji Stores".)
- Conflicting claim status or ownership dispute → `conflict`, never auto.

**Merge**: pick surviving record (highest confidence, else oldest `first_seen_at`),
union provenance rows, apply field precedence (§14 hierarchy, configurable per
field in a `field_precedence` table), snapshot both records into `merges`, keep the
merged record as a tombstone with `merged_into_id` so old external ids resolve.
`unmerge` restores from the snapshot. Every merge is audit-logged.

---

## 8. Admin screens

| Route | Purpose | Roles |
|---|---|---|
| `/` | Dashboard: totals, by source/city/category/confidence, failures, source health, last sync | all |
| `/sources`, `/sources/[id]` | CRUD, licence + robots view, rate limits, retention, test connection | data_engineer (edit), compliance_admin (approve) |
| `/sources/[id]/compliance` | Approve / restrict / block, evidence, event history | compliance_admin only |
| `/imports/new` | Wizard: source → scope (city/bbox/categories) → mode → dry-run preview | data_engineer |
| `/imports/[id]` | Live progress, logs, stats, pause/resume/cancel/retry-failed | data_engineer |
| `/review` | Queue with filters; approve/reject/edit/quarantine/close/request-verification | reviewer |
| `/review/[id]` | Record detail: map, field provenance table, source evidence, raw payload | reviewer |
| `/duplicates` / `/duplicates/[id]` | Side-by-side compare, matched vs conflicting fields, merge/split, undo | reviewer |
| `/taxonomy` | Canonical categories + source mappings + unmapped queue | data_engineer |
| `/exports/new`, `/exports/[id]` | Filters, preview, schema validate, generate, download, history | export_operator |
| `/privacy` | Deletion requests, suppressions, consent withdrawals | compliance_admin |
| `/survey` | Mobile-first field entry form incl. consent capture | field_agent |
| `/audit` | Read-only audit log search | read_only_auditor |

UI built via the `design-master` workflow (frame → decide → distinctive → build →
audit) since it's a real interface, not a scaffold.

---

## 9. API specification

Versioned under `/api/v1`, OpenAPI auto-generated. Bearer JWT (15 min) + rotating
refresh cookie. All list endpoints: `?page&size&sort&q&` typed filters; cursor
pagination on large sets.

```
POST   /auth/login · /auth/refresh · /auth/logout · /auth/me
GET    /sources                       POST /sources
GET    /sources/{id}                  PATCH /sources/{id}
POST   /sources/{id}/approve          POST /sources/{id}/block
POST   /sources/{id}/test-connection
GET    /sources/{id}/compliance-events
POST   /imports/dry-run               POST /imports
GET    /imports · /imports/{id} · /imports/{id}/logs · /imports/{id}/stats
POST   /imports/{id}/pause · resume · cancel · retry-failed
POST   /files                         # multipart upload → raw_files
POST   /files/{id}/map-columns · /files/{id}/preview · /files/{id}/import
GET    /businesses · /businesses/{id} · PATCH /businesses/{id}
GET    /businesses/{id}/provenance · /businesses/{id}/raw
POST   /businesses/{id}/approve · reject · quarantine · close · suppress
GET    /duplicates · /duplicates/{id}
POST   /duplicates/{id}/merge · /duplicates/{id}/reject
POST   /merges/{id}/undo
GET    /taxonomy/categories · /taxonomy/mappings · /taxonomy/unmapped
POST   /taxonomy/mappings
POST   /exports · GET /exports · /exports/{id} · /exports/{id}/download
POST   /deletion-requests · POST /claims · POST /closure-reports
POST   /surveys · POST /submissions/owner · POST /submissions/user
GET    /health · /ready · /metrics
```

Role checks are declarative dependencies (`require(Role.compliance_admin)`), and
there is a test that asserts every route has an explicit role guard.

---

## 10. Background jobs (Celery)

Queues: `fetch · parse · normalise · geo · dedupe · quality · export · maint`.

| Task | Trigger | Idempotency key |
|---|---|---|
| `fetch_source_unit` | import job | (batch, unit) |
| `parse_raw_file` | upload | raw_file sha256 |
| `normalise_raw_record` | after fetch | raw_record id + normaliser version |
| `geocode` / `reverse_geocode` | normalise | business + address hash |
| `map_category` | normalise | raw category key |
| `detect_duplicates` | after upsert | business id + dedupe version |
| `score_quality` | after change | business id + scorer version |
| `validate_image_rights` | image insert | image id |
| `build_export` | API | export id |
| `purge_expired_raw` | beat, hourly | — |
| `check_source_freshness` | beat, daily | — |
| `reconcile_closures` | beat, daily | — |
| `attribution_report` | beat, weekly | — |
| `refresh_robots_cache` | beat, 6-hourly | — |

All tasks: idempotent (versioned keys mean a version bump forces recompute),
`autoretry_for` transient errors with jittered exponential backoff, cooperative
cancellation via a Redis flag checked between units, checkpoints written per unit
so a killed worker resumes rather than restarts.

Beat runs under the same Windows task as the worker (single-process `--beat` is
avoided; separate task, see §13).

---

## 11. Export format

`export-schemas/locz-export-v1.json` (JSON Schema) is the contract; the exporter
validates its own output against it before writing the checksum.

Record fields: exactly the §23 list. Plus enforced defaults per §24:
`claim_status=unclaimed`, `verification_status` never above `source_verified`,
`listing_type=directory_listing`, `products_disabled=true`, `reviews_disabled=true`,
`owner_contact_unconfirmed=true`.

Formats: `csv` (RFC 4180, UTF-8 BOM optional), `jsonl`, `json`, `geojson`
(FeatureCollection; obfuscated records emit locality centroid + a
`location_accuracy` that says so).

Every export writes a directory:

```
exports/<export_id>/
  manifest.json     # export id, schema_version, created_at, filters, record_count,
                    # source_summary[], licence_summary[], attribution_requirements[],
                    # excluded_count + reasons, sha256 of data file
  data.<ext>
  ATTRIBUTION.txt   # ready-to-display attribution block (e.g. "© OpenStreetMap
                    # contributors, ODbL 1.0")
  SHA256SUMS
```

Records failing compliance/eligibility are excluded and counted, never silently
dropped.

---

## 12. Security design

- Argon2id password hashing; JWT access 15 min; refresh token rotation with reuse
  detection (reuse → revoke whole family); session revocation table.
- RBAC roles: `super_admin, compliance_admin, data_engineer, reviewer, field_agent,
  export_operator, read_only_auditor`. Source approval = compliance_admin only.
  Export generation = export_operator/super_admin only.
- Uploads: extension + magic-byte MIME check, size cap, stored outside webroot with
  generated names, sha256, pluggable malware-scan hook (Windows Defender CLI by
  default), never served back raw.
- CSRF on cookie-authenticated routes; strict CORS allowlist; security headers.
- Rate limiting on auth and write endpoints.
- Secrets: `.env` (gitignored) validated at boot by pydantic-settings; sources
  store `secret_ref` names only. Logs redact by key-name allowlist.
- Backups: nightly `pg_dump -Fc` to `./var/backups`, retention 14, restore script
  + a documented restore drill.
- Structured JSON logs with correlation ids; no secrets, no personal data in logs.
- Local-only binding by default (`127.0.0.1`); nothing exposed to the network
  unless you opt in.

---

## 13. Deployment on this machine (Windows background service)

Prereqs installed by `infrastructure/windows/bootstrap.ps1`:
PostgreSQL 18 (port 5433) + PostGIS, Memurai, Python venv, Node deps.

Four Scheduled Tasks registered by `install-tasks.ps1` (run once, elevated):

| Task | Trigger | Command |
|---|---|---|
| `LocZEngine-API` | At startup (delay 60 s) | `run-api.ps1` → uvicorn on 127.0.0.1:8080 |
| `LocZEngine-Worker` | At startup (delay 75 s) | `run-worker.ps1` → celery worker, all queues |
| `LocZEngine-Beat` | At startup (delay 90 s) | `run-beat.ps1` → celery beat (the "cron") |
| `LocZEngine-Health` | At startup + repeat every 5 min, indefinitely | `healthcheck.ps1` → restarts any dead task, writes to health log |

Task settings on all four:
- Run whether user is logged on or not, with highest privileges.
- `RestartCount 999`, `RestartInterval 1m`.
- `ExecutionTimeLimit 0` (no kill).
- Start only if network available: **off** (file imports must work offline).
- Stop if the computer switches to battery: **off**.
- Multiple instances: **IgnoreNew**.

Net effect: starts with Windows, runs whenever the machine is on, self-heals,
survives reboots. Admin UI at `http://127.0.0.1:3000`, API at `:8080/docs`.
Management: `.\infrastructure\windows\ctl.ps1 {status|start|stop|restart|logs|uninstall}`.

Recurring work itself (source refresh, retention purge, freshness checks) is
scheduled inside **Celery beat**, not as separate Windows tasks — one place to see
and change the schedule.

Linux VPS parity is kept via the docker-compose file; nothing in the app depends on
Windows-specific APIs.

---

## 14. Testing strategy

- **Unit** (fast, no DB): normalisers, phone/address parsing, name similarity,
  category mapping, dedupe scoring, quality scorers, licence rules, export
  serialisation, OSM tag→canonical mapping.
- **Integration** (real PG 18 + PostGIS + Redis, `testcontainers` or a dedicated
  `locz_engine_test` DB): migrations up/down, PostGIS distance queries, adapters
  against recorded fixtures (`respx`), file imports, celery tasks eager+broker mode,
  RBAC matrix per route, export generation.
- **Compliance** (the ones that must never regress):
  - blocked/pending/unknown source → adapter refuses;
  - denylisted domain rejected even if a row is force-marked approved;
  - robots disallow → refuses;
  - rate limit exceeded → backs off, does not burst;
  - `redistribution_allowed=false` source → records excluded from export;
  - retention sweeper purges payloads at TTL;
  - image without confirmed rights → never downloaded, never exported;
  - `do_not_publish` / suppressed / deletion-requested → absent from every format;
  - attribution text present in every manifest that needs it;
  - import path cannot produce `verification_status > source_verified`;
  - static check: no direct HTTP client import under `app/sources/`.
- **E2E**: the exact 10-step scenario in §28 of the brief, run in CI, ending with a
  re-import of the export into a throwaway LocZ-compatible schema + checksum verify.

Coverage gate on `compliance/`, `exports/`, `deduplication/` at 90 %.

---

## 15. Implementation phases

| Phase | Contents | Rough size |
|---|---|---|
| **0 — Host setup** | PG 18 + PostGIS on 5433, Memurai, venv, Windows tasks, health loop, `.env` | small |
| **1 — Foundation** | Project skeleton, config, auth + RBAC, audit log, source registry + compliance gate + denylist + robots + rate limit, raw storage + retention, CSV/XLSX import with column mapping, canonical schema + migrations, admin shell + dashboard | large |
| **2 — OSM** | Overpass adapter (bounded, tag-scoped, cached), PBF/Geofabrik extract path, tag→canonical mapping tables, attribution, geospatial validation + accuracy classification | medium |
| **3 — Normalise & dedupe** | Name/phone/address/hours/URL normalisers, category mapping + unmapped queue, blocking + scoring + decision, merge/unmerge UI, quality scores | large |
| **4 — Review & export** | Review queue + actions, provenance viewer, export builder + manifest + checksums + JSON Schema validation, export history | medium |
| **5 — Field & partner** | Survey form + consent capture, partner uploads + agreements, owner submission + claim workflow, user suggestions | medium |

Each phase ends with its tests green and a short demo path you can click through.

---

## 16. Risks and assumptions

**Assumptions**
1. LocZ consumes files/API; the engine never writes to LocZ. Confirmed by brief.
2. India-first: phone normalisation defaults to `+91`, addresses assume Indian
   pincode/locality structure. Other regions need config, not code.
3. Single-machine deployment; no HA requirement at this stage.
4. You will supply, per source, the licence and permission evidence — the engine
   records and enforces, it does not adjudicate legality.

**Risks**
| Risk | Mitigation |
|---|---|
| Overpass throttling / bans on large jobs | Tile-bounded queries, narrow tag filters, result caching, mandatory delay, PBF path for anything city-scale+, self-hosted Overpass config option |
| PostGIS unavailable for PG 18 on Windows at install time | Verified at bootstrap; fallback is PG 17 + PostGIS, or PG 16 + PostGIS. I'll confirm during Phase 0 before writing migrations. |
| Dedup false merges damage data | Conservative auto-merge rules, name-only never merges, full snapshot + undo, all merges audited |
| Government dataset licences vary wildly | Per-dataset registration required; no bulk "gov = open" assumption; export excludes non-redistributable |
| Personal numbers published as business contacts | Shared-number detection, review flag, no auto-publish of suspect numbers |
| Home-business address exposure | `location_obfuscation` modes, locality-only default for `home_business` |
| Scope creep toward "just scrape it" | Denylist in code + import-linter contract + compliance tests; adding a prohibited source requires editing source code and breaking CI |
| Windows Scheduled Task silently dies | Health task every 5 min restarts and logs; dashboard shows worker heartbeat |
| Machine off → missed schedules | Beat schedules are catch-up-on-start for maintenance tasks; ingestion is explicitly operator-triggered anyway |

---

## Awaiting approval

Reply with **approve** to start Phase 0 + Phase 1, or tell me what to change
(scope, stack, phase order, dedup thresholds, roles, export fields).
