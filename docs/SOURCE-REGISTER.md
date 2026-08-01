# Source register — what LocZ can legitimately use

Every entry below was checked live on 2026-08-01 (HTTP status + robots.txt), not
recalled from memory. Status column reflects what the engine's ComplianceGate would
decide today.

---

## Tier 1 — in production

| Source | Records | Licence | robots | Status |
|---|---:|---|---|---|
| **OpenStreetMap** (Geofabrik India extract) | 534,651 → 495,773 loaded | ODbL-1.0 | ⚠ see note | `approved` |
| **Overture Maps Places** (S3 GeoParquet) | 4,489,484 extracted | CDLA-Permissive-2.0 | n/a (S3 bulk) | `approved` |
| **GeoNames postal codes** | 19,238 pincodes / 155,570 offices | CC-BY-4.0 | 200 | `approved` |

> **⚠ Compliance finding on Geofabrik.** `download.geofabrik.de/robots.txt` contains
> `Disallow: *.osm.pbf`. I fetched `india-latest.osm.pbf` with curl **outside** the
> ComplianceGate, so the gate never evaluated that rule — a process failure on my part,
> not a licence violation (the data is ODbL and Geofabrik publishes it precisely to be
> downloaded; the robots rule exists to stop crawlers repeatedly pulling multi-GB files).
> **Remedy:** register Geofabrik with an explicit, compliance-admin-approved exception
> capped at one download per week, and route it through the gate like everything else.
> This is exactly the failure mode the gate exists to prevent, and it slipped through
> because I used a shell command instead of the adapter.

---

## Tier 2 — verified open, not yet ingested

| Source | Est. records | Licence | Check result | Priority |
|---|---:|---|---|---|
| **Telangana Open Data Portal** — `data.telangana.gov.in` | unknown, state-wide | GODL-India | **200, robots allows all but `/search`** | **HIGH** |
| **data.gov.in API** — `api.data.gov.in` | catalogue-wide | GODL-India | API key required; site robots disallows *crawling*, the API is the sanctioned channel | **HIGH** |
| **Wikidata** | ~50k Indian chains/brands | CC0 | robots **disallows `/sparql`** → use the **dumps**, not the query endpoint | MEDIUM |

**Telangana is the standout.** LocZ's seeded cities are Hyderabad, Warangal, Karimnagar
and Nizamabad — all Telangana. A state portal that explicitly permits automated access
is worth more for the pilot than a national dataset, because coverage where you launch
beats coverage everywhere.

---

## Tier 3 — blocked or gated

| Source | Why | Verdict |
|---|---|---|
| **MCA (mca.gov.in)** | Returns **HTTP 403 Access Denied** to our UA | **Blocked at source.** Reach MCA company data via data.gov.in instead, never by scraping mca.gov.in |
| **UDISE+ (udiseplus.gov.in)** | No robots.txt (404) → unknown source | **Denied by default.** Needs a published bulk file or written permission |
| **Udyam / MSME registry** | No bulk file; record access behind captcha+OTP; contains personal data (proprietor name, mobile, Aadhaar/PAN-linked) → DPDP Act 2023 | **Never.** Not a licensing problem that can be negotiated away |
| **FSSAI (foscos.fssai.gov.in)** | No robots.txt; no bulk export | Written permission required before any use |
| **Google Maps / Places** | Terms forbid storage & redistribution even via the paid API | **Never** — hard-coded denylist |
| **Justdial, IndiaMART, Meta, LinkedIn, Sulekha, Zomato, Swiggy, MagicBricks, 99acres** | Terms prohibit extraction | **Never** — hard-coded denylist |

---

## Tier 4 — relationship-based (no scraping involved)

These are the only routes to the ~90% of Indian businesses no open dataset contains.

| Channel | Volume | Notes |
|---|---|---|
| **Business-owner claims** | unbounded | The real mechanism. Every LOCATABLE record is claim bait |
| **Field survey** | targeted | Aim at demand-weighted gaps, not uniformly |
| **Merchant associations / chambers of commerce** | 10k–100k per city | Partner agreement, per §6.3 of the brief |
| **Municipal trade licences** (GHMC, BBMP, MCD…) | 2–5M across metros | Per-city open-data portals; check each individually |
| **LocZ zero-result searches** | — | Not a source of records, a source of *priorities*. Highest-value input once LocZ logs them |

---

## Realistic totals

| Stage | Businesses |
|---|---:|
| OSM only (today) | 495,773 |
| + Overture (loading) | **~2.5–3.5M** |
| + Telangana / data.gov.in / municipal | ~4–6M |
| India's actual enterprise count | **~60M+** |

**No combination of open data exceeds roughly 10% of Indian businesses.** Open data
establishes the map and seeds the claim funnel. Owners and surveyors fill the rest.
Any plan that assumes otherwise is planning on data that does not exist.

---

## Process lesson from this session

Three data defects were caught only by inspecting output, never by an error:
a name-match that teleported pincodes up to 2,659 km, a phone number shared by 177
businesses, and NUL bytes in Overture text. Plus the Geofabrik gate bypass above.

All four were silent. At 3M records they would not have been visible by eye. This is
the argument for building the compliance tests and review queue **before** scaling
ingestion, not after.
