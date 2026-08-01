# LocZ — Vision Alignment Assessment

Repo: `github.com/Iamvlnmurthy/locznew` @ default branch, shallow clone, read 2026-08-01.
Method: structural review — Prisma schema (1,603 lines, ~60 models), API module tree,
web route tree, admin route tree, Flutter feature tree, home page, key detail models.
**Not** a line-by-line read of every service. Percentages are judgement calls against the
vision document's own section structure, weighted by how central each is to the stated
product principle.

---

## Headline

# ~58% aligned

Split very unevenly:

| Layer | Alignment | Comment |
|---|---:|---|
| **Platform foundation** (auth, RBAC, location, moderation, safety, chat, media, audit, i18n, search infra, jobs) | **~85%** | Genuinely strong. Better than most products at this stage. |
| **Seller-to-buyer half** of the marketplace (§3.1) | **~70%** | Listings, businesses, offers, jobs, rentals, events all modelled and routed. |
| **Buyer-to-seller half** (§3.2, §10, §11) — *the stated differentiator* | **~20%** | Data model exists. The loop that makes it a product does not. |
| **Presentation of the dual model** (§4, §5, §18) | **~10%** | The "I Want to Buy / I Want to Sell" home screen does not exist in web or mobile. |
| **Mobile app** (§22) | **~35%** | Skeleton only — 8 feature folders, no requirement/business/offer screens. |
| **Seller typing & home businesses** (§2.3, §13) | **~15%** | No `businessType`/`sellerType` field anywhere. No Home Business badge. |
| **Inventory & availability** (§9, §14) | **~20%** | No stock states, no bulk/CSV import, no fulfilment options. |

The codebase is a **well-built local classifieds platform**. The vision describes a
**two-sided local demand/supply network**. The gap between those two things is almost
entirely the buyer-demand loop and the seller typing around it.

---

## What is genuinely strong

- **Location model is excellent and vision-aligned.** Country → State → District → City →
  Locality → Pincode, with `geography(Point,4326)` on City, Locality, Pincode, Address,
  Business and Listing. `geo` maintained by a DB trigger (ADR-0009), all spatial I/O via
  `GeoRepository` (ADR-0003). The home feed already accepts `pincode` and falls back to it
  for visitors outside launched cities — §6 is close to fully delivered.
- **Moderation and child safety are unusually mature.** `ModerationAction`, `ModeratorNote`,
  `Report`, `BannedKeyword`, `BlockedImageHash`, `MediaSafetyCase`, `MediaSafetyAccessLog`,
  `UserSuspension`, `AuditLog`, plus a `child-safety-readiness` release gate and a
  `PROTECTED_HASH_PROVIDER_APPLICATION` doc. §20 is effectively **fully implemented**.
- **Typed listing model done right.** ADR-0004 — one `Listing` table with per-type
  extension tables (`MarketplaceDetail`, `BuyerRequirementDetail`, `OfferDetail`,
  `JobDetail`, `ServiceDetail`, `RentalDetail`, `EventDetail`). This is exactly what §12
  asks for and it avoids the "generic listing model" trap §E warns about.
- **Payments correctly absent.** No wallet, escrow, checkout or commission anywhere.
  `Plan`/`Subscription`/`FeaturedPlacement` exist as schema-only, inactive. §2.2 respected.
- **Ratings correctly deferred**, matching §19's precondition list.
- **Offers are well modelled.** `OfferDetail` with `startsAt`/`endsAt`, indexed for
  "offers valid now", redemption counts, in-store vs online. §17 is ~75% at the data layer.
- **i18n enforced by CI** (`check:i18n`, `check:hardcoded`), en/te/hi columns on Category,
  City, Locality. §29.9 respected structurally.
- **Search discipline.** Meilisearch as derived index only (ADR-0005); ADR-0015 — "the
  database path narrows the search; it never widens it".

---

## The central gap: the buyer-demand loop is a stub

The vision names this the most important differentiator (§3.2). Here is what exists and
what does not.

**Exists:**
- `ListingType.BUYER_REQUIREMENT`
- `BuyerRequirementDetail { budgetMin, budgetMax, requiredBy, quantity, preferredCondition }`

**Does not exist:**

| §11 requirement | Status |
|---|---|
| Seller notified of matching requirements | **Missing** — no matching service, no job, no `NotificationType` for it |
| Seller response model (Available / different price / can arrange / made to order…) | **Missing** — no `RequirementResponse` model at all |
| Response → chat | **Blocked** — `ConversationContext` is `LISTING_ENQUIRY \| BUSINESS_ENQUIRY \| JOB_ENQUIRY`. No requirement context. |
| Anti-spam on responses (limits, relevance, duplicate prevention) | **Missing** |
| `responses received`, `fulfilled`, `active/closed` on the requirement | **Missing** — `BuyerRequirementDetail` has none of these |
| Search radius on the requirement | **Partial** — `Listing.serviceRadiusKm` exists but is generic |
| Reference images | Inherited from `ListingMedia` ✓ |

`SearchSubscription` is the closest existing machinery — a saved-search model that could be
repurposed as the matching substrate. That is the cheapest credible path to §11.

**Verdict on §10/§11: ~20% — schema seed only, no product.**

---

## The second gap: the dual-intent home screen

§4 requires "I Want to Buy" and "I Want to Sell" as two equal primary choices, and
explicitly forbids hiding buyer requirements inside a listing-type dropdown.

Current web home (`apps/web/src/app/page.tsx`) is: hero + search form + category tiles +
`/feed` sections. There is no buy/sell intent split. The only place `buyerRequirement`
appears in the web app is `post/listing-type-fields.tsx` — **it is inside the listing-type
dropdown, the exact anti-pattern §4 names.**

There is no `/wanted`, `/requirements`, or `/offers` route. Web routes are:
`/`, `/search`, `/post`, `/ad/[slug]`, `/b/[slug]`, `/c/[slug]`, `/in/[city]`, `/business`,
`/chats`, `/dashboard`, `/location`, `/notifications`, `/register`, `/report`, `/signin`,
plus statics.

**Verdict on §4/§5/§18: ~10%.**

---

## Section-by-section scoring

| § | Vision area | Score | Classification |
|---|---|---:|---|
| 2.1 | Offline business discovery | 55% | Partial — businesses + listings exist; product-level store search weak |
| 2.2 | No commissions / no payments | 100% | Fully implemented (by correct absence) |
| 2.3 | Home businesses given identity | 15% | **Missing** — no seller/business type, no badge |
| 2.4 | Offline product search | 50% | Partial — search exists; store inventory not indexed as products |
| 3.1 | Seller-to-buyer | 70% | Largely implemented |
| 3.2 | Buyer-to-seller | 20% | **Schema only** |
| 4 | Dual-intent home screen | 10% | **Missing** |
| 5 | Home feed structure | 40% | Feed module + sections exist; wrong section set |
| 6 | Location-first | 90% | Near-complete; distance display needs verification |
| 7 | Universal search + type grouping | 55% | Search exists; cross-type grouping tabs unverified/likely absent |
| 8 | Business storefronts | 65% | `Business` + hours + holidays + `ServiceArea` + `/b/[slug]` + manage UI |
| 9 | Product & inventory discovery | 20% | No stock states, no bulk/CSV, no fast-create |
| 10 | Buyer requirements as a major section | 20% | Detail table only |
| 11 | Seller matching & responses | 5% | **Missing entirely** |
| 12 | Typed listing types | 85% | ADR-0004 done well |
| 13 | Seller types | 15% | **Missing** — no type field |
| 14 | Availability & fulfilment | 20% | `ListingStatus` covers lifecycle, not availability/fulfilment |
| 15 | Direct contact, no transaction | 75% | `ContactPreference`, `showPhonePublicly`, conversations |
| 16 | Chat safety | 60% | Chat + block + report solid; contextual fraud cautions unverified |
| 17 | Offers & deal feed | 70% | Strong model, no dedicated route/feed surface |
| 18 | Local feed with typed cards | 35% | Feed exists; typed card distinction unverified |
| 19 | Trust & reputation | 45% | Verification + counters; response-rate/reply-time absent |
| 20 | Moderation & safety | 95% | **Fully implemented**, best area in the codebase |
| 21 | User roles | 80% | `RoleName` enum + `UserRole` + RBAC module |
| 22 | Mobile experience | 35% | 8 features: auth, feed, listings, search, post, chat, notifications, account, location. No requirement/business/offer screens |
| 23 | Web SEO surfaces | 75% | `sitemap.ts`, `robots.ts`, `/in/[city]`, `/c/[slug]`, `/b/[slug]`, `/ad/[slug]` |
| 24 | Admin console | 60% | users/businesses/listings/moderation/reports/safety/categories/audit/system — no demand analytics, no requirements, no verification queue |
| 25 | Analytics | 25% | Counters on entities; no zero-result search capture, no supply-demand gap reporting |
| 26 | Future capability headroom | 80% | Schema-ready, correctly inactive |

---

## What this means for the Pincode Business Data Engine

Three direct consequences:

1. **The engine feeds the 55%-aligned half.** Seeding directory businesses strengthens
   §2.1 and §8 — real supply so the feed isn't empty at launch. It does nothing for the
   20%-aligned buyer-demand half, which is LocZ's actual differentiator. Worth being
   clear-eyed that seeding is a cold-start fix, not the product gap.

2. **The engine's biggest LocZ-side blocker (`Business` has no `businessType`) is also a
   vision gap (§2.3, §13).** One migration solves both: add `businessType` /`sellerType`
   with `HOME_BUSINESS`, `RETAIL_STORE`, `SERVICE_PROVIDER`, `MANUFACTURER`, `WHOLESALER`,
   `INDIVIDUAL` etc. The engine can populate it from OSM tags on import.

3. **Zero-result searches and unfulfilled requirements (§25) are the engine's best targeting
   input.** Once LocZ captures them, the engine should prioritise pincode/category cells by
   observed local demand rather than a flat 2–3 target. That is a Phase-3 feedback loop
   worth designing the schema for now.

---

## Confidence and limits

- Schema, route trees, module trees and the home page were read directly — those findings
  are solid.
- Scores for §7 grouping, §16 contextual cautions, §18 card typing and §24 admin depth are
  inferred from structure, not from reading every service and component. They could move
  ±15 points either way on closer inspection.
- I did not run the app, the acceptance scripts, or the test suite.
