# Task: make LocZ able to receive directory business data

**Run this in the LocZ repository (`Iamvlnmurthy/locznew`), not in the data engine.**

Reference repo: <https://github.com/Iamvlnmurthy/loczbusinessdata> — the LocZ Pincode
Business Data Engine. It holds **3.9 million Indian businesses** resolved to pincode,
category and locality, from OpenStreetMap (ODbL-1.0) and Overture Maps
(CDLA-Permissive-2.0). It exports files. It does not and must not write to LocZ.

Your job is the LocZ side: schema changes, an import path, and the UI rules that keep
imported data honest.

---

## Context you need before touching anything

Verified against `apps/api/prisma/schema.prisma` on 2026-08-01. **Re-verify before
editing — the schema may have moved on.**

LocZ today cannot store a single directory record. Four blockers:

| Blocker | Current state | Consequence |
|---|---|---|
| `Business.ownerId` | `String @db.Uuid` — NOT NULL | A directory business has no owner. Insert fails. |
| No pincode relation on `Business` | only `Listing` has `pincodeCode` | Cannot answer "businesses in 500081" |
| No claim concept | zero occurrences of "claim" in the repo | Nothing for an owner to claim |
| No provenance fields | no source / licence / attribution columns | **Legal problem** — ODbL and CDLA require attribution wherever data is shown |

There is also a taxonomy mismatch: LocZ's `businesses` category branch has three
children (`business-restaurants`, `business-clinics`, `business-shops`); the engine
uses 46 local-business categories.

---

## Phase 1 — Schema (do this first, it unblocks everything)

### 1.1 Allow unowned listings

```prisma
model Business {
  ownerId String?  @db.Uuid          // was: String
  owner   User?    @relation("BusinessOwner", fields: [ownerId], references: [id])
}
```

Prefer nullable over a synthetic "system owner" user: a null owner is honest, a fake
owner is not, and `ownerId IS NULL` becomes the natural "unclaimed" query.

Audit every read path that assumes `business.owner` is non-null. Expect breakage in
business detail, dashboard and any owner-notification code.

### 1.2 Pincode relation

```prisma
model Business {
  pincodeCode String?  @db.VarChar(6)
  pincode     Pincode? @relation(fields: [pincodeCode], references: [code])
  @@index([pincodeCode, categoryId, isActive])
}
```

`Listing` already has this with the comment *"Ads in my pincode — the primary query
once the platform is open everywhere."* `Business` needs the same.

### 1.3 Provenance — required for legal display

```prisma
model Business {
  locZId              String?  @unique @db.VarChar(16)   // "LOCZ-A3K9-2M7X"
  externalDirectoryId String?  @unique @db.VarChar(80)   // "osm:n123" / "ovt:..."
  sourceName          String?  @db.VarChar(80)
  sourceType          String?  @db.VarChar(40)
  sourceRecordId      String?  @db.VarChar(120)
  sourceUrl           String?  @db.VarChar(400)
  licenceName         String?  @db.VarChar(60)
  attributionText     String?  @db.VarChar(200)
  confidenceScore     Int?     @db.SmallInt
  completenessScore   Int?     @db.SmallInt
  pincodeConfidence   Decimal? @db.Decimal(3,2)
  locationAccuracy    String?  @db.VarChar(24)
  lastSeenAt          DateTime?
}
```

`locZId` is the engine's permanent public identifier, format `LOCZ-XXXX-XXXX` in
Crockford base32 (no I, L, O or U, so it survives being read aloud on a phone call and
typed by an owner). **It never changes and is never reused.** Use it as the claim
handle and print it on any "claim your store" material.

`attributionText` is effectively mandatory — ODbL and CDLA-Permissive both require
attribution wherever the data appears.

`locationAccuracy` governs display. See Phase 3.

### 1.4 Business type — also closes vision gaps §2.3 and §13

```prisma
enum BusinessType {
  RETAIL_STORE
  FOOD_SERVICE
  SERVICE_PROVIDER
  HOME_BUSINESS      // vision §2.3: home bakers, tailors, makers
  HOSPITALITY
  INSTITUTION
  WHOLESALER
  MANUFACTURER
  PUBLIC_SERVICE     // renders without a claim button
}

model Business { businessType BusinessType? }
```

The vision review found seller typing at ~15% implemented and the Home Business badge
missing entirely. This field serves both needs; the engine already derives it for all
3.9M records.

### 1.5 Publication type and claim status

```prisma
enum PublicationType { DIRECTORY_LISTING  OWNER_CREATED  COMMUNITY_ADDED }
enum ClaimStatus     { UNCLAIMED  CLAIM_PENDING  CLAIMED  OWNERSHIP_DISPUTED }

model Business {
  publicationType       PublicationType @default(OWNER_CREATED)
  claimStatus           ClaimStatus     @default(UNCLAIMED)
  productsEnabled       Boolean @default(true)    // false on import
  offersEnabled         Boolean @default(true)    // false on import
  reviewsEnabled        Boolean @default(true)    // false on import
  ownerContactConfirmed Boolean @default(false)
}
```

### 1.6 Verification needs a source tier

```prisma
enum VerificationStatus {
  UNVERIFIED
  SOURCE_VERIFIED    // NEW — in a government registry, or agreed by 2+ sources
  PENDING
  VERIFIED           // owner-verified ONLY
  REJECTED
}
```

**An import must never set anything above `SOURCE_VERIFIED`.** Enforce in the import
service and, ideally, a DB check constraint.

---

## Phase 2 — Import endpoint

### Export format

Each export is a directory:

```
data.csv | data.json | data.jsonl | data.geojson
manifest.json        filters, record_count, source_summary, licence_summary,
                     attribution_requirements, exclusion_reasons, sha256
ATTRIBUTION.txt      ready to display
SHA256SUMS
```

Schema version: `locz-export-v1`. Key fields per record:

```
locz_id  external_directory_id  pincode  pincode_confidence  pincode_method
display_name  resolved_name  business_type  category  subcategory
public_phone  phone_line_type  public_email  website
address_line_1  address_line_2  locality  mandal  city  district  state
latitude  longitude  location_accuracy  opening_hours
source_name  source_type  source_url  licence_name  attribution_text
confidence_score  completeness_score  freshness_score  tier
claim_status  verification_status  publication_type
products_disabled  offers_disabled  reviews_disabled  owner_contact_unconfirmed
```

### Import rules — all mandatory

1. **Verify `sha256` against `SHA256SUMS` before importing.** Reject on mismatch.
2. **Upsert on `locz_id`**, falling back to `external_directory_id`. **Never match on
   name** — Indian business names repeat heavily ("Sri Balaji Stores" is not one shop).
3. **Never overwrite a `CLAIMED` or `VERIFIED` business.** Imported values for those
   become *suggested updates* requiring admin approval.
4. **Missing stays missing.** A null phone renders as "Phone not listed", never a dead
   call button. Do not substitute, infer, or default.
5. **Do not infer products.** Category never implies inventory.
6. Imported records arrive `UNCLAIMED`, `UNVERIFIED`, `DIRECTORY_LISTING`, with
   products, offers and reviews disabled.
7. **Record the import batch** — export id, checksum, record count, timestamp — so an
   import can be traced and reversed.

### Admin surface

`/admin/imports` — upload or point at an export directory, preview, validate schema,
dry-run diff (new / updated / conflicting), then commit. Show the manifest's
`exclusion_reasons` so operators can see what was withheld and why.

---

## Phase 3 — Display rules

These protect user trust. Treat them as requirements, not styling.

**`locationAccuracy` decides what the UI may claim:**

| Value | May show distance? | Directions? |
|---|---|---|
| `exact_storefront` (~5–25 m) | yes | yes |
| `building` (~5–15 m) | yes | yes |
| `locality` (~1–3 km) | **no** | **no** — show "in <locality>" only |
| `unknown` | no | no |

A locality-accurate record shown as a precise pin sends a user to the wrong street.

**Tier decides the card:**

| Tier | Card |
|---|---|
| `CONTACTABLE` | Full card, call button |
| `LOCATABLE` | Directions + distance + **"Is this your business? Claim it"**, no call button |
| `HELD` | Never exported; should never reach LocZ |

**Attribution must be visible** wherever directory data appears — business page footer
is sufficient:
`© OpenStreetMap contributors (ODbL 1.0) · © Overture Maps Foundation (CDLA-Permissive 2.0)`

**Never present imported data as verified.** No badge, no tick, no "verified" wording
until an owner claims and confirms.

---

## Phase 4 — Claim flow

This is what converts a directory into a marketplace, and it does not exist yet.

```prisma
model BusinessClaim {
  id          String   @id @db.Uuid
  businessId  String   @db.Uuid
  claimantId  String   @db.Uuid
  method      String   @db.VarChar(30)   // phone_otp | document | field_visit
  evidenceUrl String?  @db.VarChar(400)
  status      String   @db.VarChar(20)   // pending | approved | rejected
  reviewedBy  String?  @db.Uuid
  reviewedAt  DateTime?
  createdAt   DateTime @default(now())
  business Business @relation(fields: [businessId], references: [id])
  @@unique([businessId, claimantId])
  @@index([businessId, status])
}
```

Flow: owner finds their business (search by name, or enter their `LOCZ-XXXX-XXXX`) →
requests claim → OTP to the listed number, or document upload if no number → admin
approves → `claimStatus = CLAIMED`, `ownerId` set, products/offers/reviews enabled,
`publicationType = OWNER_CREATED`.

On approval the owner should be prompted to correct anything wrong — that correction
is the point of the whole exercise, and it is the only path to data no scraper can
produce.

---

## Phase 5 — Categories

The engine's 46 categories do not exist in LocZ. Two options:

**Preferred:** seed the 46 under the existing `businesses` node, with `nameTe` and
`nameHi` populated so the i18n CI check passes.

**Fallback:** store the engine's category slug as a string and map on read. Cheaper
now, but users cannot browse or filter by it, which is most of the directory's value.

Mapping 46 → the existing 3 loses the distinction between a kirana, a hardware shop
and a tyre dealer. Don't.

---

## Constraints

1. **Do not modify the data engine repo.** It is a separate project.
2. **Preserve existing functionality.** Nullable `ownerId` touches many read paths —
   find them all.
3. **Use Prisma migrations.** No manual SQL against a live database.
4. **Keep i18n and accessibility intact** — CI enforces both.
5. **Do not activate payments.**
6. **Do not fabricate data** to fill gaps in imported records.
7. **Add tests** for: checksum verification, upsert-not-duplicate, claimed-record
   protection, null-phone rendering, attribution presence.

---

## Acceptance criteria

- [ ] A `locz-export-v1` CSV imports without error
- [ ] Re-importing the same file changes nothing (idempotent)
- [ ] A `CLAIMED` business is not overwritten by an import
- [ ] A record with no phone shows "Phone not listed", not a dead button
- [ ] A `locality`-accuracy record shows no distance and no directions
- [ ] Attribution is visible on every imported business page
- [ ] No imported business displays as verified
- [ ] `/businesses?pincode=500081` returns results
- [ ] An owner can claim a business via `LOCZ-XXXX-XXXX` and correct its details
- [ ] Existing owner-created businesses are unaffected

---

## Suggested order

1. **Phase 1.1–1.3** — minimum to accept one record. Start here.
2. **Phase 2** import endpoint + admin screen
3. **Phase 3** display rules
4. **Phase 1.4–1.6** business type, publication type, verification tier
5. **Phase 5** categories
6. **Phase 4** claim flow

Stop after step 1 and import a small pilot export before building the rest. The
engine has 3.9M records and **not one has ever been loaded into LocZ** — find what
breaks on 500 records, not on 3.9 million.
