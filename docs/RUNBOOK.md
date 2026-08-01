# Runbook

Operational notes for running and refreshing the engine. Written for whoever picks
this up next, including future me.

---

## Refresh cadence

There is no always-on worker, deliberately. The bulk sources ship as monthly extracts,
so a scheduled service would spend its life processing an empty queue. Re-run by hand
when a new release lands.

| Source | Cadence | Command |
|---|---|---|
| Overture Places | monthly | bump `OVERTURE_RELEASE`, then `extract_overture.py` → `load_overture.py` |
| OpenStreetMap | monthly | `extract_pbf.py` → `load_places.py` → `load_businesses.py` |
| GeoNames pincodes | ~quarterly | re-download `IN.zip`, `seed.py` |
| Telangana registries | monthly files | verification matching only |

After any load: `fix_phones.py`, then `python tests/test_compliance.py`.

**Always bump the release deliberately.** A new Overture release changes record counts,
which changes export checksums. Pinning is why `.env` carries `OVERTURE_RELEASE`.

---

## Order matters

Geography must exist before businesses, or every record resolves against a stale or
uncorrected centroid:

```
seed.py            pincodes + taxonomy + source registry
extract_pbf.py     OSM → businesses.jsonl + places.jsonl
load_places.py     places → PostGIS
fix_centroids.py   bounded centroid correction     ← before any business load
load_businesses.py OSM → canonical
extract_overture.py / load_overture.py
fix_phones.py      validate + suppress shared numbers
finalise.py        identity → leads → names → tests → export
```

---

## Things that will bite you

**Bulk UPDATEs hold the table for a long time.** The Overture locality pass is ~3.4M
nearest-neighbour probes and runs for several minutes. Any DDL issued during it queues
behind it *and blocks every reader in the meantime*. `finalise.py` waits for idle
rather than taking the lock — do the same for ad-hoc DDL, and cancel with
`pg_cancel_backend()` rather than letting a blocked statement pile up readers behind it.

**The monitor opens a connection per poll.** During a heavy write it adds several
concurrent readers. Harmless normally, worth stopping during a big load.

**DuckDB globbing on the Overture S3 bucket is slow if unfiltered.** Always keep the
`bbox.xmin/ymin` predicates so row groups are pruned remotely rather than downloaded.

**Overture text contains NUL bytes.** Postgres text columns reject them. `clean()` in
`load_overture.py` strips control characters — do not remove it.

---

## Failure modes seen in practice

Each of these was silent. None raised an error. All are now covered by
`tests/test_compliance.py`.

| Symptom | Cause | Guard |
|---|---|---|
| Pincode moved 2,659 km | Name matched a same-named village in another state | Correction bounded to 60 km from GeoNames + 120 km from district anchor |
| One phone on 177 businesses | Call-centre number published as a shop line | Suppress any number used by >3 businesses |
| `sunrise-sunset` opening hours | Valid OSM syntax, meaningless for a shop | Implausible-value rejection in the hours normaliser |
| Load crashed mid-stream | NUL bytes in Overture strings | `clean()` strips control characters |
| Downloaded a file robots.txt disallows | Used `curl` directly instead of the adapter | Route every fetch through `ComplianceGate` |

---

## Checking health

```bash
python scripts/monitor.py            # http://127.0.0.1:8420
python tests/test_compliance.py      # 22 assertions; exit 1 on any failure
```

```sql
-- has anything lost its provenance?
SELECT count(*) FROM businesses WHERE source_id IS NULL OR attribution_text IS NULL;

-- is any phone published on more than 3 businesses?
SELECT public_phone, count(*) FROM businesses WHERE public_phone IS NOT NULL
GROUP BY 1 HAVING count(*) > 3 ORDER BY 2 DESC LIMIT 5;

-- coverage by tier
SELECT tier, count(*) FROM businesses GROUP BY 1;
```

---

## Exporting for LocZ

```bash
python scripts/export.py --district Hyderabad --tier CONTACTABLE --format csv
python scripts/export.py --pincode 500081 500084 --format geojson
```

Output lands in `var/exports/<uuid>/` with `data.*`, `manifest.json`,
`ATTRIBUTION.txt`, `SHA256SUMS`.

**LocZ cannot import these yet.** See [LOCZ-MIGRATION-SPEC.md](LOCZ-MIGRATION-SPEC.md):
`Business.ownerId` is NOT NULL, there is no pincode FK and no claim concept. Steps 1–3
of that spec are the minimum to accept a single record.

---

# Session log — 2026-08-01

## Bugs that were silent

Every one of these was syntactically valid, raised no error, and did the wrong
thing quietly. Listed because the pattern matters more than the individual fix:
at 4M rows, "it ran without error" tells you almost nothing.

| Bug | Symptom | Fix |
|---|---|---|
| Unbounded name match | pincode relocated 2,659 km | bound to 60 km + district anchor |
| Shared phone number | one number on 177, later 20,617 businesses | suppress any number on >3 |
| NUL bytes in Overture text | load crashed mid-stream | strip control chars at read |
| `'pub'` inside `'public_school'` | finance companies filed as restaurants | token-aware matching |
| Alternates outranking primary | `credit_union` lost to `public_school` | resolve primary alone first |
| `LEFT JOIN … ON true` | cross join, 49 min without finishing | scalar EXISTS on an index |
| `\b` in a Postgres regex | every name rule matched zero rows | `\y` — `\b` is backspace in POSIX |
| `WHERE query LIKE '%foo%'` | `pg_cancel_backend` cancelled itself | add `pid <> pg_backend_pid()` |
| Suppression ran before later loads | 5,314 businesses shared one number | **caught by the compliance suite** |
| Hardcoded password default | leaked to a public repo in 22 files | env-only, no fallback; password rotated |

Only one was caught by automation. The rest were found by reading output,
watching elapsed time, or noticing a count of zero. That asymmetry is the
argument for `tests/test_compliance.py` growing every time something slips.

## Performance lessons

- **Drive joins from the smaller side.** The Overture dedup ran 25 minutes from
  the 3.5M side; the registry match ran in seconds from the 190k side.
- **Watch for skew before choosing a join key.** Three districts hold 4.3M of
  5.7M register rows, so "same district" was no filter at all where it mattered.
- **Do not queue four bulk writers against one 4M-row table.** They serialise
  behind each other and autovacuum, turning minutes into hours. Batch the
  post-load phase into a single pass.
- **A monitor that full-scans the table it monitors starves the pipeline.**
  Back off polling when a bulk write is running.
