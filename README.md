# LocZ Pincode Business Data Engine

A standalone, compliance-first pipeline that builds a local-business directory for
**LocZ** from openly licensed data — and never touches the LocZ codebase.

It currently holds **~4 million Indian businesses** resolved to pincode, category and
locality, plus **5.7 million government registry entries** used as verification evidence.

---

## What it does

```
bulk extracts          →  raw records, immutable + checksummed
   ↓ normalise            names, phones (E.164), URLs, hours
   ↓ categorise           source tag → 46-category LocZ taxonomy (data, not code)
   ↓ resolve pincode      ladder: stated postcode 0.97 → named place 0.90 → nearest 0.75
   ↓ deduplicate          phone match, or name similarity within 150 m
   ↓ score                completeness · confidence · freshness
   ↓ tier                 CONTACTABLE · LOCATABLE · HELD
   ↓ export               CSV / JSON / JSONL / GeoJSON + manifest + SHA-256
```

**Tiering is the core idea.** A record with a working phone is CONTACTABLE. One with a
verified location but no phone is LOCATABLE — LocZ shows directions and a "claim this
business" prompt, never a dead call button. Everything else is HELD and never exported.

---

## What it will not do

Hard-coded, enforced by `ComplianceGate`, not by convention:

- No Google Maps / Places, Justdial, IndiaMART, Meta, LinkedIn, Sulekha, Zomato,
  Swiggy, MagicBricks, 99acres — the denylist is source code; no admin can whitelist it
- No CAPTCHA solving, proxy rotation, fingerprint spoofing or auth bypass
- Unknown sources are **denied by default**
- No fabricated data. An absent field stays null — never inferred from category
- No imported record is ever marked owner-verified
- Personal data (e.g. `employer_name` in state registries) is dropped at ingestion

---

## Current data

| | |
|---|---:|
| Businesses | **3,995,468** |
| Publishable phone numbers | ~2,500,000 |
| Pincodes covered | **19,186 of 19,238** (99.7%) |
| Pincode centroids corrected | 13,707 |
| Named places (Area Resolver) | 307,762 |
| Categories | 47, with 245+ source-tag mappings |
| Registry entries (verification) | 5,736,966 |
| Government-verified (`source_verified`) | 10,374 |
| EV charging stations | 1,511 |
| Compliance tests | **24 / 24 passing** |

Shared corporate numbers suppressed: **119,692** — one number appeared on 20,617
businesses. A number that is not the shop's own line is worse than no number.

Sources: OpenStreetMap via Geofabrik (ODbL-1.0), Overture Maps Places
(CDLA-Permissive-2.0 — itself aggregating Meta, Microsoft, Foursquare, AllThePlaces),
GeoNames postal codes (CC-BY-4.0), Open Charge Map (CC-BY-4.0), Telangana Open Data
Portal (GODL-India, verification only).

---

## Security

**Credentials come from `.env` only.** There is no fallback default anywhere in the
code. An earlier revision carried a hardcoded Postgres password as a default in 22
scripts and pushed it to this public repository; that password has been rotated and
the defaults removed, but **it remains in git history**. Treat anything ever committed
here as public. `.env` is gitignored — verify with `git check-ignore .env` before
adding secrets.

```bash
cp .env.example .env     # then fill in LOCZ_DSN and any API keys
git check-ignore .env    # must print .env
```

Scripts exit with a clear error if `LOCZ_DSN` is unset rather than silently
connecting with a guessed password.

---

## Setup

Requires PostgreSQL 18 + PostGIS 3.6 and Python 3.12.

```bash
python -m venv .venv && .venv/Scripts/pip install osmium psycopg[binary] duckdb
createdb -p 5433 locz_engine
psql -p 5433 -d locz_engine -f sql/001_schema.sql
psql -p 5433 -d locz_engine -f sql/002_exports.sql
psql -p 5433 -d locz_engine -f sql/003_leads.sql
cp .env.example .env      # then edit
```

```bash
# geography first — everything else depends on it
python scripts/seed.py                 # 19,238 pincodes + taxonomy + source registry
python scripts/extract_pbf.py          # India OSM extract → businesses + places
python scripts/load_places.py          # place index into PostGIS
python scripts/fix_centroids.py        # bounded centroid correction
python scripts/load_businesses.py      # OSM → canonical
python scripts/extract_overture.py     # Overture India → parquet
python scripts/load_overture.py        # Overture → canonical, deduplicated
python scripts/fix_phones.py           # validate + suppress shared numbers

python scripts/monitor.py              # http://127.0.0.1:8420
python scripts/export.py --pincode 500081 --format csv
```

---

## Layout

```
scripts/     pipeline stages, each runnable and idempotent
sql/         schema — compliance enforced by CHECK constraints, not convention
docs/        findings and specs (read these before changing the pipeline)
sample-data/ exported samples
infrastructure/windows/  optional scheduled-task wrappers
```

### Documents worth reading first

| Doc | Why |
|---|---|
| [PINCODE-CENTROID-AUDIT.md](docs/PINCODE-CENTROID-AUDIT.md) | GeoNames pincode centroids are wrong by a median 18.6 km; 69.4% share coordinates with another pincode. Explains why targeting is by area name, not centroid |
| [SOURCE-REGISTER.md](docs/SOURCE-REGISTER.md) | Every source checked live: licence, robots.txt, verdict |
| [LOCZ-MIGRATION-SPEC.md](docs/LOCZ-MIGRATION-SPEC.md) | **LocZ cannot receive this data yet.** The schema changes it needs |
| [LOCZ-VISION-ALIGNMENT.md](docs/LOCZ-VISION-ALIGNMENT.md) | How closely LocZ matches its own product vision (~58%) |

---

## Known limitations

- **Open data covers well under 10% of Indian businesses.** ~60M enterprises exist;
  every open source combined reaches maybe 4–6M. The rest only ever comes from owner
  claims and field survey. Any plan assuming otherwise is planning on data that does
  not exist.
- **Phone coverage is the binding constraint**, not location. Coordinates are ~99%
  usable; phones are the field that decides whether a listing is actionable.
- **Overture's internal duplicates have not been measured.** Cross-source dedup runs;
  within-Overture dedup does not yet.
- **139,300 Overture records have no category** and are not imported — a business with
  no category cannot be filed under a taxonomy, and guessing from the name would be
  fabrication.
- Telangana registries carry no coordinates. They are used for verification only.
  **Gap analysis by mandal does not work**: the register uses GHMC administrative
  circles ("circle 10", "circle 37") while our locality field holds OSM place names.
  The two do not align, so the join produced nonsense in both directions and the
  table was dropped rather than left looking like fact. Fixing it needs a
  circle-to-locality bridge that does not currently exist.
- Registry matching is **exact normalised name within district**, not fuzzy. Trigram
  matching spilled 15 GB of temp in seven minutes because Hyderabad district alone
  holds 1.86M register entries. Exact equality is a hash join; it trades recall
  (5.5% matched) for precision, which is the right trade when the output is a
  verification claim.

---

## Licence and attribution

Code: see `LICENSE`. **Data is not covered by it.** Every exported record carries its
source licence and attribution, and the export manifest aggregates them. Downstream
consumers must display attribution:

```
© OpenStreetMap contributors (ODbL-1.0)
© Overture Maps Foundation (CDLA-Permissive-2.0)
```
