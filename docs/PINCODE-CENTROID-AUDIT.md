# Pincode centroid audit — the geographic foundation is not usable for targeting

Run 2026-08-01. Data: GeoNames `IN.zip` (155,570 post-office rows → 19,238 codes) —
the exact dataset LocZ's `import-pincodes.ts` uses. Validation: OSM/Nominatim gazetteer,
stratified sample of 60, 1 request/second.

---

## Part 1 — Offline classification (all 19,238 pincodes)

| Risk tier | Meaning | Count | Share |
|---|---|---:|---:|
| **A severe** | 20+ pincodes share one identical coordinate | 1,553 | 8.1% |
| **B shared** | 2–19 pincodes share one identical coordinate | 3,971 | 20.6% |
| **C collapsed** | all offices on one point, coordinate unique | 2,408 | 12.5% |
| **D distinct** | own coordinate, offices genuinely spread | 11,306 | 58.8% |

**5,524 pincodes (28.7%) cannot be assigned by nearest-centroid at all** — their centroid
is shared with at least one other code, so "nearest" is a coin flip.

Largest coordinate collisions:

| Coordinate | Pincodes | Place |
|---|---:|---|
| 13.2257, 77.5750 | **98** | Bengaluru, Karnataka |
| 22.5553, 88.3558 | 62 | Kolkata, West Bengal |
| 18.9808, 72.8338 | 54 | Mumbai, Maharashtra |
| 19.3600, 73.3279 | 48 | Thane, Maharashtra |
| 22.4656, 88.7803 | 48 | North 24 Parganas, WB |
| 9.2841, 76.9290 | 43 | Pathanamthitta, Kerala |

All 98 Bengaluru pincodes resolve to a single point. Radius search anchored there is
meaningless for the whole city.

---

## Part 2 — Validation against OSM (n=50 resolved, 10 not found)

Offset between the GeoNames centroid and OSM's location for the same postcode:

| Band | Count | Share |
|---|---:|---:|
| < 2 km (good) | 3 | **6.0%** |
| 2–5 km (marginal) | 4 | 8.0% |
| 5–15 km (bad) | 14 | 28.0% |
| **> 15 km (unusable)** | **29** | **58.0%** |

**Median offset 18.63 km. p90 51.44 km. Max 100.57 km.**

By risk tier — note the surprise:

| Tier | n | Median offset | Max |
|---|---:|---:|---:|
| A severe | 14 | 30.85 km | 98.63 km |
| B shared | 16 | 28.14 km | 52.25 km |
| C collapsed | 10 | 5.41 km | 51.44 km |
| **D distinct** ("healthy") | 10 | **8.68 km** | **100.57 km** |

**Tier D — the 58.8% I classified as healthy — has a median 8.68 km error and the single
worst case in the sample (509201 Turkaplly, 100.57 km).** Offline classification does not
predict accuracy. There is no safe subset.

Also: **10 of 60 (17%) have no postcode representation in OSM at all**, so they cannot be
corrected this way either.

---

## How much of this is certain

**Certain**, verified independently:
- The coordinate collisions are arithmetic on the source file. 98 Bengaluru pincodes
  genuinely share one point.
- For 506001 (Warangal) the GeoNames centroid is wrong by 22.7 km. Confirmed three ways:
  Nominatim place lookup for Hanamkonda, and an Overpass business count that returned
  **5 records at the GeoNames centroid vs 714 at the true location** — a 140× difference.

**Less certain:**
- Nominatim's postcode point is itself derived from OSM address density and can be sparse
  or skewed. So the 18.63 km median measures *disagreement between two sources*, not
  proven GeoNames error in every case. The true error is probably smaller than 18.63 km
  but, on the Warangal evidence, is clearly large enough to break targeting.
- n=50 gives wide confidence intervals. The direction is unambiguous; the exact percentages
  are not.

---

## Consequences

1. **Pincode centroids cannot decide where to search.** Confirmed by the 5-vs-714 result.
2. **Nearest-pincode assignment must be removed, not merely down-weighted.** It was already
   the weakest rung on the ladder (0.50); this audit says it should not exist.
3. **LocZ is affected too.** Its radius search anchors on these centroids
   (`Pincode.geo`), and `activate-india.ts` derives all 638 district-city coordinates from
   the centroid of each district's pincodes. Errors of this size propagate into "businesses
   near me". **This should be passed to whoever works on LocZ.**

---

## The fix

**Build a corrected geometry table offline from the Geofabrik India OSM extract (ODbL).**

1. Download `india-latest.osm.pbf` (~1.3 GB, one-time).
2. Extract every `place=city|town|village|suburb|neighbourhood` node with a name
   (~hundreds of thousands, covers the country).
3. Match each pincode's GeoNames office names against those places within its
   district — district is reliable in GeoNames even when coordinates are not.
4. Derive per pincode: a corrected centroid, a real spread radius, and a matched-place list.
5. Store `centroid_source` ∈ `geonames | osm_place_match | osm_postal_boundary | manual`
   plus `centroid_offset_km`, so every correction is auditable and reversible.

Zero ongoing network cost, no rate limits, ODbL-clean, and it also produces the named-place
index the Area Resolver needs. Where an OSM `boundary=postal_code` relation exists (rare in
India but not absent), it supersedes the derived centroid.

Pincodes that match nothing stay flagged `centroid_unverified` and are excluded from
automated targeting until a human or a field survey resolves them.

---

## What this changes in the plan

**Search by area name and OSM geometry. Account by pincode.**

The pincode remains the coverage ledger — a complete, closed, non-overlapping set of 19,238
cells you can prove you have finished. It stops being the thing that decides where to point
the scraper.

New Phase 1b, before any source adapter runs:

```
Geofabrik India PBF  ->  place index  ->  pincode geometry correction
                                       ->  Area Resolver (GPS -> area, text -> area)
                                       ->  pincode assignment ladder:
                                             exact_source_pincode   0.97
                                             named_place_match      0.90
                                             osm_postal_boundary    0.88
                                             nearest_named_place    0.75
                                             reverse_geocoded       0.65
                                             nearest_pincode        REMOVED
```

---

# APPENDIX — Correction executed 2026-08-01

Source: `india-latest.osm.pbf` (Geofabrik, ODbL), 290,436,783 objects, 19.4 min.
Yielded **307,762 named places** and **534,651 businesses**, fully offline.

## Result

| Centroid source | Pincodes | Share |
|---|---:|---:|
| `osm_place_match` (name matched, bounded ≤60 km) | 9,742 | 50.6% |
| `osm_postal_boundary` (place states the postcode, bounded <100 km) | 3,965 | 20.6% |
| `geonames` (unique coordinate, no match found — kept as-is) | 2,687 | 14.0% |
| `unverified` (shared coordinate, no safe correction — **excluded from targeting**) | 2,844 | 14.8% |

**13,707 pincodes corrected (71.2%). 16,394 targetable (85.2%).**
Post-correction offset: **avg 17.71 km, max 99.67 km, 4,176 within 5 km.**

## Spot checks

| Pincode | Place | Moved | Method |
|---|---|---:|---|
| 500081 | Madhapur, Hyderabad | 0.33 km | osm_place_match |
| 506001 | Subedari, Warangal | **23.20 km** | osm_place_match |
| 560001 | Bangalore G.P.O. | 27.59 km | osm_postal_boundary |
| 700001 | Council House St, Kolkata | — | unverified (withheld) |

506001's 23.20 km correction independently reproduces the 22.7 km error measured
against Nominatim before any of this ran. Two unrelated methods, same answer.

## Two bugs found and fixed during the run

1. **Unbounded name matching moved pincodes up to 2,659 km.** Village names repeat
   across India, so a name match alone is not evidence. Fixed by requiring a candidate
   to sit within 60 km of the GeoNames position *and* within 120 km of its district
   anchor (median of that district's postcode-verified pincodes). Reverted and re-ran.
2. **Stated-postcode matches were also unbounded**, leaving a 1,398 km outlier from a
   mis-tagged OSM place. 59 corrections ≥100 km reverted to `unverified`.

Both classes now fail closed: a correction that cannot be justified is not applied,
and the pincode is marked non-targetable instead of being moved to a guess.
