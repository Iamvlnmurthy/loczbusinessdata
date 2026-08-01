# LocZ repo audit — what it means for the Pincode Business Data Engine

Repo: `github.com/Iamvlnmurthy/locznew` (public, ~30 MB, TypeScript monorepo)
Read at: 2026-08-01, default branch, shallow clone.

---

## 1. What LocZ actually is

| Aspect | Reality |
|---|---|
| Stack | **NestJS + Prisma 7 + TypeScript**, npm workspaces monorepo (`apps/api`, `apps/web`, `apps/admin`, `apps/mobile` (Flutter), `packages/*`) |
| Database | **PostgreSQL 18 + PostGIS 3.6** — `postgis/postgis:18-3.6` in both dev and prod compose. Extensions: `postgis, pg_trgm, unaccent, citext` |
| Geo convention | `geography(Point,4326)`, declared `Unsupported` in Prisma; all spatial I/O via `GeoRepository` (ADR-0003); `geo` populated by a **DB trigger**, not app code (ADR-0009) |
| Search | Meilisearch as a derived index, never source of truth (ADR-0005) |
| Schema size | 1,603-line `schema.prisma`, ~60 models, 16 ADRs in `DECISIONS.md` |
| Discipline | High. Idempotency keys, refresh-token families, release gates, acceptance scripts, i18n checks (en/te/hi) |

**Your "Postgres 18" instruction was correct** — it matches LocZ exactly. Good call.

---

## 2. The pincode dataset — the engine's foundation

```prisma
model Pincode {
  code          String   @id @db.VarChar(6)   // PK is the code itself
  name          String                        // most common office name
  districtName  String
  mandal        String?
  stateName     String
  latitude      Decimal  @db.Decimal(10,7)
  longitude     Decimal  @db.Decimal(10,7)
  geo           Unsupported("geography(Point, 4326)")?
  officeCount   Int      @default(1)          // coarseness proxy
  cityId        String?  @db.Uuid             // ONLY set for launched cities
  isServiceable Boolean  @default(true)
}
```

Sourced from **GeoNames `IN.zip`** via `apps/api/prisma/import-pincodes.ts` — ~155,000
post offices collapsed to **~19,300 unique codes**, centroid = mean of that code's offices.
The data is **not committed** to the repo; it's imported from GeoNames.

### What this means for the engine

1. **I can build the pincode table independently** — same GeoNames source, same collapse
   logic. No DB dump or API access to your LocZ instance needed for Phase 1. The `code`
   is a stable natural key shared by both systems, so `locz_pincode_id` in the revised
   prompt is simply **the 6-digit code**. No ID mapping layer required.
2. **There are no pincode polygons.** Only a centroid + `officeCount`. So §4 of the
   revised prompt ("exact pincode boundaries where legally available") has no data behind
   it today. The engine must be radius-first, with `officeCount` as the radius driver:
   a code with 40 offices spans far more ground than one with 1.
3. **No urban/rural classification and no population** on `Pincode`. `City.population`
   exists but only for launched cities. §2 and §6 of the prompt need this — I'll have to
   **derive** it (office density + OSM feature density + city membership), not read it.

---

## 3. The blocking problem: LocZ's `Business` model cannot hold directory records

```prisma
model Business {
  ownerId            String   @db.Uuid   // NOT NULL → requires a real User
  cityId             String   @db.Uuid   // NOT NULL → requires a launched City
  categoryId         String   @db.Uuid
  verificationStatus VerificationStatus  // UNVERIFIED | PENDING | VERIFIED | REJECTED
  // no pincode relation at all
  // no source / attribution / licence / confidence fields
  // no claim concept anywhere in the repo
}
```

Five concrete conflicts with the revised prompt's §21 export contract:

| Prompt requires | LocZ has | Severity |
|---|---|---|
| `locz_pincode_id`, `pincode` on the business | **No pincode FK on `Business`.** Only `Listing` has `pincodeCode` | **Blocker** |
| Unowned directory listings | `ownerId` NOT NULL | **Blocker** |
| Every pincode covered (~19,300) | `cityId` NOT NULL, but `Pincode.cityId` is null outside launched cities | **Blocker** |
| `claim_status: unclaimed`, later claimable | **No claim concept exists anywhere in the repo** | **Blocker** |
| `source_name`, `source_url`, `attribution_text`, `confidence_score`, `publication_type: directory_listing` | None of these fields exist | **Blocker** |
| `verification_status: source_verified` | Enum has only UNVERIFIED/PENDING/VERIFIED/REJECTED | Minor (map to UNVERIFIED) |

**`Listing` is a much better fit than `Business`:** it already has `pincodeCode` (with a
dedicated index `[pincodeCode, status, publishedAt]` commented *"Ads in my pincode — the
primary query once the platform is open everywhere"*), `districtId`, `stateId`,
`localityId`, `postalCode`, `geo`, `subcategoryId`, and a **`BUSINESS_LISTING`** value in
`ListingType`. But it still requires `ownerId` and `cityId` NOT NULL.

→ **LocZ needs a schema change to receive this data.** That is a LocZ-side task, and the
prompt forbids me from doing it in this project. It must be planned and handed over.

---

## 4. Category taxonomy mismatch

LocZ's tree is a **classifieds/marketplace** taxonomy seeded in `seed.ts`:
`electronics, vehicles, furniture-home, jobs, services, real-estate-rentals, local-offers,
businesses, events` — and the `businesses` branch has exactly **three** children:
`business-restaurants`, `business-clinics`, `business-shops`.

The revised prompt asks for **46 local-business categories** (grocery, kirana, hardware,
tyre & battery, agricultural supplies, home food sellers…). These do not exist in LocZ.

Two mapping layers are therefore needed, not one:

```
OSM tag  →  engine canonical category (46)  →  LocZ category id (existing tree)
```

The second mapping is lossy today — 46 collapses into 3. Either LocZ's `businesses`
branch gets expanded (a LocZ-side seed change), or exports carry the engine's category as
a string and LocZ maps it on import.

`taxonomy.json` in `prisma/data/` is **not** the category tree — it's attribute option
lists (vehicles, CAMERA, LAPTOP, MOBILE). The tree lives in `seed.ts`.

---

## 5. Other findings

- **Docker is LocZ's dev path** (`npm run docker:up`) but Docker is **not installed on
  this machine**. My native Windows plan still stands for the engine.
- Node ≥20.11 required; this box has Node 24. Fine.
- `AreaCorrection` model exists — a user-reported location-correction flow. Useful signal
  source for the engine later, and a precedent for how LocZ handles crowd input.
- `ADR-0013` defines what the platform refuses to carry; `ADR-0016` — "a count the product
  cannot deliver is not a count" — is directly relevant to coverage-target honesty.
- No `claims`, no `directory` module, no import/ingestion surface in `apps/api` today.

---

## 6. Consequences for the engine design

1. **Stack decision is now live.** LocZ is TypeScript/NestJS/Prisma. The revised prompt
   (§24) still says Python/FastAPI. Matching LocZ would let the engine reuse Prisma models,
   the geo trigger pattern, category seeds, and the shared `packages/*` types — at the cost
   of leaving Python's stronger data/geo tooling (Polars, Shapely, GeoPandas, RapidFuzz,
   osmium) behind.
2. **Pincode boundaries must be radius-first**, sized from `officeCount` + derived density,
   with optional OSM `boundary=postal_code` polygons where they exist (sparse in India).
3. **The 46-category taxonomy is the engine's own**, seeded from the prompt, with an
   explicit engine→LocZ mapping table that is allowed to be incomplete and reports gaps.
4. **§15 (compare against existing LocZ data) is cheap right now** — LocZ has no seeded
   businesses yet beyond demo fixtures, so the comparison layer can be built but will
   mostly return `new_business` during the pilot.
5. **A LocZ-side migration spec is a required deliverable** of the design phase, even
   though I won't implement it here.
