# Move business search from Meilisearch to Postgres

**For the LocZ repo.** Replaces the Meilisearch backend of `BusinessSearchService`
with Postgres full-text + PostGIS. The service interface does not change, so no
caller changes.

---

## Why

**The container limit forces it.** `locz-meilisearch` is capped at 512 MB. There are
now ~3M businesses in Postgres; indexing them would OOM the container and take
listings search down with it.

**The sync step is the durable reason.** On 2026-08-03, 400 businesses sat correctly
in Postgres and business search returned zero results, silently, because the derived
index had not been rebuilt. `search.controller.ts` already carries a comment about
the same class of failure:

> *"a silent queue is how a rebuild once appeared to succeed while leaving the index empty."*

A generated column has no rebuild to forget.

**Measured on 4M rows** (the engine's own copy, same PostGIS 18):

| | Meilisearch | Postgres FTS |
|---|---|---|
| Index size | GBs | **124 MB** |
| Extra process | yes | **no** |
| Rebuild step | yes | **none** |
| `"medical"` + 10 km radius | — | **124 ms** |
| `"hardware"` / `"kirana"` / `"hotel"` | — | **4–10 ms** |

Keep Meilisearch for **listings** — small, user-typed, typo tolerance genuinely matters
when someone types "iphon 13". Businesses are browsed by category and proximity.

---

## 1. Migration

```sql
-- prisma/migrations/<ts>_business_search_postgres/migration.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- The document IS the row. Nothing to rebuild, nothing to drift.
ALTER TABLE businesses
  ADD COLUMN "searchDoc" tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', coalesce(name, '')),            'A') ||
    setweight(to_tsvector('simple', coalesce("sourceRecordId", '')), 'D') ||
    setweight(to_tsvector('simple', array_to_string(keywords, ' ')), 'B')
  ) STORED;

CREATE INDEX businesses_search_doc_idx ON businesses USING gin ("searchDoc");

-- typo tolerance: trigram on the name, used only as a fallback
CREATE INDEX businesses_name_trgm_idx ON businesses USING gin (name gin_trgm_ops);

-- proximity
CREATE INDEX IF NOT EXISTS businesses_geo_idx ON businesses USING gist (geo);
```

`simple` rather than `english`: business names are proper nouns and Indic
transliterations, where English stemming does more harm than good ("Medicals" must
not stem to "medic").

Category name is intentionally **not** in the document. Indexing it makes every
pharmacy match the query "store", which floods results — filter on `categoryId`
instead, which the service already supports.

---

## 2. Replace the service internals

`apps/api/src/search/business-search.service.ts` — same signature, new body.

```ts
async search(params: {
  query: string; cityId?: string; pincode?: string; categoryId?: string;
  businessType?: string; latitude?: number; longitude?: number;
  radiusKm?: number; page: number; limit: number;
}): Promise<{ ids: string[]; total: number }> {
  const { query, page, limit } = params;
  const offset = (page - 1) * limit;
  const hasGeo = params.latitude != null && params.longitude != null;
  const radius = (params.radiusKm ?? 10) * 1000;

  const rows = await this.prisma.$queryRaw<{ id: string; total: bigint }[]>`
    WITH matched AS (
      SELECT b.id,
             ts_rank(b."searchDoc", plainto_tsquery('simple', ${query})) AS rank,
             ${hasGeo ? Prisma.sql`
               ST_Distance(b.geo, ST_MakePoint(${params.longitude},
                                               ${params.latitude})::geography)`
                      : Prisma.sql`0`} AS metres
      FROM businesses b
      WHERE b."deletedAt" IS NULL
        AND b."isActive"
        AND (${query} = '' OR b."searchDoc" @@ plainto_tsquery('simple', ${query}))
        ${params.cityId     ? Prisma.sql`AND b."cityId" = ${params.cityId}::uuid` : Prisma.empty}
        ${params.pincode    ? Prisma.sql`AND b."pincodeCode" = ${params.pincode}` : Prisma.empty}
        ${params.categoryId ? Prisma.sql`AND b."categoryId" = ${params.categoryId}::uuid` : Prisma.empty}
        ${hasGeo ? Prisma.sql`AND ST_DWithin(b.geo,
                     ST_MakePoint(${params.longitude}, ${params.latitude})::geography,
                     ${radius})` : Prisma.empty}
    )
    SELECT id, count(*) OVER () AS total
    FROM matched
    -- relevance first, then distance: the nearest irrelevant shop is still irrelevant
    ORDER BY rank DESC, metres ASC
    LIMIT ${limit} OFFSET ${offset}`;

  return { ids: rows.map(r => r.id), total: Number(rows[0]?.total ?? 0) };
}
```

### Typo tolerance

`plainto_tsquery` is exact-token. Where Meilisearch forgave "medicl", add a fallback
that only runs when the strict query returns nothing:

```ts
if (rows.length === 0 && query.length >= 4) {
  // pg_trgm similarity — slower, so it never runs on the common path
  return this.fuzzySearch(params);       // WHERE name % ${query} ORDER BY similarity DESC
}
```

Set `SET pg_trgm.similarity_threshold = 0.3` for the fuzzy path.

### The methods that become no-ops

```ts
async indexBusiness(_id: string)  { /* generated column — nothing to do */ }
async removeBusiness(_id: string) { /* generated column — nothing to do */ }
async reindexAll()                { return { indexed: await this.prisma.business.count() }; }
```

Keep `reindexAll` returning a count so the admin endpoint and its tests still pass.
`status()` should report `indexedDocuments === businesses` by definition — drift is
no longer possible, which is the point.

---

## 3. What to verify

- [ ] `"medical"` in Warangal returns pharmacies, nearest first
- [ ] `"kirana"` does not return astrologers *(a real bug found on 2026-08-03)*
- [ ] Empty query + `categoryId` filter still browses
- [ ] `pincode` filter returns only that pincode
- [ ] `"medicl"` falls back to fuzzy and still finds something
- [ ] p95 latency under 150 ms with 3M rows
- [ ] Listings search still works — Meilisearch untouched

---

## 4. What is given up

**Typo tolerance is worse.** Meilisearch is genuinely better here; the trigram
fallback is a rougher tool. Acceptable for businesses, which are mostly reached by
category and proximity — not acceptable for listings, which is why they stay.

**No built-in synonyms or ranking rules.** Postgres weights (A/B/D above) are cruder
than Meilisearch's ranking pipeline.

**Relevance tuning is manual.** `ts_rank` is not as tunable as Meilisearch's rules.

Worth it for the category that has 3M rows, a 512 MB ceiling, and a rebuild step that
has already failed silently once.
