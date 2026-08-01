# Exports ready for LocZ

Three bundles, produced by `scripts/export.py`. **Copy these to whichever machine
runs LocZ** — they are the deliverable; the 15 GB database is not.

| Folder | Records | Scope |
|---|---:|---|
| `warangal-5000-csv` | 5,000 | Warangal district, 17 pincodes |
| `hyderabad-5000-csv` | 5,000 | Hyderabad district |
| `pincode-500081-500084-csv` | 727 | Two Hyderabad pincodes — smallest, use this first |

Each folder holds:

```
data.csv          the businesses
manifest.json     filters used, record count, sources, licences, what was excluded and why
ATTRIBUTION.txt   licence text LocZ MUST display
SHA256SUMS        verify before importing
```

## Before importing

1. **Check the checksum.** `sha256sum -c SHA256SUMS` — reject on mismatch.
2. **Read `manifest.json`.** It lists what was withheld and why. The Warangal
   bundle excluded 659 records because their pincode centroid is unverified.
3. **Display the attribution.** ODbL and CDLA-Permissive both require it wherever
   the data appears.

## Import rules

Upsert on `locz_id`, never on name — Indian business names repeat heavily.
Never overwrite a claimed or verified business. A null phone renders as
"Phone not listed", never a dead call button. A `locality`-accuracy record must
not show a distance.

Full contract: [../docs/LOCZ-INTEGRATION-PROMPT.md](../docs/LOCZ-INTEGRATION-PROMPT.md)

## LocZ cannot import these yet

`Business.ownerId` is NOT NULL and there is no pincode column on `Business`, so
the first row fails to insert. Apply steps 1-3 of the migration spec first.
