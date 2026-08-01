# LocZ migration spec — what LocZ needs before it can receive this data

**For the LocZ repo (`Iamvlnmurthy/locznew`), not this one.** Hand this to whoever
works on LocZ. The engine does not and must not modify LocZ.

Verified against `apps/api/prisma/schema.prisma` on 2026-08-01.

---

## Why a migration is needed at all

LocZ's `Business` model cannot represent a directory listing. Four hard blockers:

| Blocker | Current schema | Consequence |
|---|---|---|
| `ownerId String @db.Uuid` | **NOT NULL** | A directory business has no owner. Cannot insert. |
| `cityId String @db.Uuid` | **NOT NULL** | Satisfiable post-`activate-india` (638 district cities), but ties every record to a district row rather than its pincode |
| No pincode relation | `Business` has **no** `pincodeCode` | Cannot answer "businesses in 500081", the engine's entire unit of work |
| No claim concept | Zero occurrences of "claim" in the repo | Nothing for an owner to claim, so the acquisition funnel has no landing point |

Plus: no `source`, `attribution`, `licence`, or `confidence` fields, so provenance
cannot survive the import — which breaks ODbL and CDLA attribution obligations.

---

## Required changes

### 1. Allow unowned directory listings

```prisma
model Business {
  ownerId String? @db.Uuid   // was: String
  // ...
  owner User? @relation("BusinessOwner", fields: [ownerId], references: [id])
}
```

Alternative if nullable is unacceptable: create one system user
(`directory@locz.internal`) and assign it. Nullable is cleaner — a null owner is
honest, a fake owner is not, and `ownerId IS NULL` is exactly the "unclaimed" query.

### 2. Pincode relation on Business

```prisma
model Business {
  pincodeCode String?  @db.VarChar(6)
  pincode     Pincode? @relation(fields: [pincodeCode], references: [code])
  @@index([pincodeCode, categoryId, isActive])   // "hardware shops in 500081"
}
```

`Listing` already has `pincodeCode` with the comment *"Ads in my pincode — the primary
query once the platform is open everywhere"*. `Business` needs the same.

### 3. Business type — serves the directory AND vision §2.3 / §13

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

model Business {
  businessType BusinessType?
}
```

The engine already derives this from source tags for all 3.9M records.

### 4. Provenance and trust

```prisma
model Business {
  externalDirectoryId String?  @unique @db.VarChar(80)  // "osm:n123" / "ovt:xxx"
  sourceName          String?  @db.VarChar(80)
  sourceType          String?  @db.VarChar(40)
  sourceRecordId      String?  @db.VarChar(120)
  sourceUrl           String?  @db.VarChar(400)
  licenceName         String?  @db.VarChar(60)
  attributionText     String?  @db.VarChar(200)   // MUST be displayed — ODbL/CDLA
  confidenceScore     Int?     @db.SmallInt
  completenessScore   Int?     @db.SmallInt
  pincodeConfidence   Decimal? @db.Decimal(3,2)
  locationAccuracy    String?  @db.VarChar(24)    // exact_storefront|building|locality
  lastSeenAt          DateTime?
}
```

`attributionText` is not optional in practice: ODbL and CDLA-Permissive both require
attribution wherever the data is shown.

`locationAccuracy` governs display. A `locality` record **must not** show a distance
or a directions button — it is accurate to ~1–3 km, not to a shopfront.

### 5. Publication type and claim flow

```prisma
enum PublicationType { DIRECTORY_LISTING  OWNER_CREATED  COMMUNITY_ADDED }
enum ClaimStatus     { UNCLAIMED  CLAIM_PENDING  CLAIMED  OWNERSHIP_DISPUTED }

model Business {
  publicationType PublicationType @default(OWNER_CREATED)
  claimStatus     ClaimStatus     @default(UNCLAIMED)
  productsEnabled Boolean @default(true)   // false for directory listings
  offersEnabled   Boolean @default(true)
  reviewsEnabled  Boolean @default(true)
  ownerContactConfirmed Boolean @default(false)
}

model BusinessClaim {
  id           String   @id @db.Uuid
  businessId   String   @db.Uuid
  claimantId   String   @db.Uuid
  method       String   @db.VarChar(30)   // phone_otp | document | field_visit
  evidenceUrl  String?  @db.VarChar(400)
  status       String   @db.VarChar(20)   // pending | approved | rejected
  reviewedBy   String?  @db.Uuid
  reviewedAt   DateTime?
  createdAt    DateTime @default(now())
  business Business @relation(fields: [businessId], references: [id])
  @@index([businessId, status])
  @@unique([businessId, claimantId])
}
```

### 6. `VerificationStatus` needs a source tier

```prisma
enum VerificationStatus {
  UNVERIFIED
  SOURCE_VERIFIED   // NEW: present in a government registry or 2+ sources
  PENDING
  VERIFIED          // owner-verified only
  REJECTED
}
```

An import must **never** set anything above `SOURCE_VERIFIED`. Enforce in code and,
ideally, a check constraint.

### 7. Category taxonomy

LocZ's `businesses` branch has three children (`business-restaurants`,
`business-clinics`, `business-shops`). The engine uses 46 local-business categories.

Either seed the 46 under the existing `businesses` node (with `nameTe`/`nameHi` to
satisfy the i18n CI check), or accept the engine's slug as a string and map on import.
Mapping 46 → 3 loses the distinction between a kirana, a hardware shop and a tyre
dealer, which is most of the directory's value.

---

## Import contract

Export format: `locz-export-v1`. Each export directory contains `data.<ext>`,
`manifest.json`, `ATTRIBUTION.txt`, `SHA256SUMS`.

Rules LocZ must honour:

1. **Verify the SHA-256** before importing.
2. **Upsert on `externalDirectoryId`**, never on name.
3. **Never overwrite a `CLAIMED` or `VERIFIED` business.** Imported values for those
   become *suggested updates* requiring admin approval.
4. **Display `attributionText`** wherever the data appears.
5. **Missing stays missing.** A null phone must render as "not listed", never as a
   dead call button.
6. **Do not infer products.** A category never implies inventory.
7. Records arrive `UNCLAIMED`, `UNVERIFIED`, `DIRECTORY_LISTING`, with products,
   offers and reviews disabled.

---

## Suggested order

1. Nullable `ownerId` + `publicationType` + `claimStatus` — unblocks insertion
2. `pincodeCode` FK + index — unblocks the primary query
3. Provenance columns — unblocks legal display
4. `businessType` — serves vision §2.3/§13 as well as the directory
5. Category expansion
6. `BusinessClaim` + claim UI — turns the directory into acquisition

Steps 1–3 are the minimum to accept a single record.
