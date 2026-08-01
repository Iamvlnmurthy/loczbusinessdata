"""LocZ export engine.

  python scripts/export.py --pincode 500081 --format csv
  python scripts/export.py --district Hyderabad --tier CONTACTABLE --format geojson
  python scripts/export.py --state Telangana --min-confidence 70 --format jsonl

Produces a directory containing the data file, a manifest, an attribution block and
SHA256SUMS. Records that fail a compliance or eligibility rule are excluded and
counted by reason - never silently dropped.
"""
import argparse, csv, hashlib, json, os, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
OUTDIR = ROOT / "var" / "exports"
def _dsn():
    """Connection string comes from the environment. No default: a hardcoded
    fallback password ends up in version control, which is how this file used to
    leak one to a public repository."""
    v = os.environ.get("LOCZ_DSN")
    if not v:
        env = Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("LOCZ_DSN="):
                    return line.split("=", 1)[1].strip()
        raise SystemExit("LOCZ_DSN is not set. Copy .env.example to .env and fill it in.")
    return v


DSN = _dsn()
SCHEMA_VERSION = "locz-export-v1"

# The export contract. Order is stable across runs; adding a field is a version bump.
FIELDS = [
    "external_directory_id", "locz_pincode_code", "pincode", "pincode_confidence",
    "pincode_method", "display_name", "resolved_name", "slug", "business_type",
    "category", "subcategory", "description", "public_phone", "phone_line_type",
    "whatsapp_available", "public_email", "website",
    "address_line_1", "address_line_2", "locality", "mandal", "city", "district",
    "state", "country", "latitude", "longitude", "location_accuracy",
    "opening_hours", "source_type", "source_name", "source_record_id", "source_url",
    "licence_name", "attribution_text", "confidence_score", "completeness_score",
    "freshness_score", "tier", "claim_status", "verification_status",
    "publication_type", "products_disabled", "offers_disabled", "reviews_disabled",
    "owner_contact_unconfirmed", "last_seen_at",
]

# Eligibility. Every clause is a reason a record may be withheld, and each is
# counted separately so the manifest can explain the shortfall.
EXCLUSIONS = [
    ("tier_held",            "b.tier = 'HELD'"),
    ("no_pincode",           "b.pincode_code IS NULL"),
    ("low_pincode_conf",     "b.pincode_confidence < 0.55"),
    ("unverified_centroid",  "p.centroid_src = 'unverified'"),
    ("no_category",          "b.category_id IS NULL"),
    ("merged_away",          "b.merged_into_id IS NOT NULL"),
    ("suppressed",           "b.lifecycle_status IN ('suppressed','deletion_requested','rejected')"),
    ("source_not_redistributable", "COALESCE(d.redistribution_allowed,false) = false"),
]


def build_where(a):
    w, args = ["1=1"], []
    if a.pincode:
        w.append("b.pincode_code = ANY(%s)"); args.append(a.pincode)
    if a.city:
        w.append("b.city ILIKE %s"); args.append(a.city)
    if a.district:
        w.append("b.district ILIKE %s"); args.append(a.district)
    if a.state:
        w.append("b.state ILIKE %s"); args.append(a.state)
    if a.category:
        w.append("c.slug = ANY(%s)"); args.append(a.category)
    if a.tier:
        w.append("b.tier = ANY(%s::export_tier[])"); args.append(a.tier)
    if a.min_confidence:
        w.append("b.confidence_score >= %s"); args.append(a.min_confidence)
    if a.min_completeness:
        w.append("b.completeness_score >= %s"); args.append(a.min_completeness)
    return " AND ".join(w), args


SELECT = """
SELECT
  b.external_id, b.pincode_code, b.pincode_code, b.pincode_confidence::float,
  b.pincode_method::text, b.display_name, COALESCE(b.resolved_name, b.display_name),
  b.slug, b.business_type, c.slug, b.subcategory, b.description,
  b.public_phone, b.phone_line_type, NULL::boolean, b.public_email, b.website,
  b.address_line_1, b.address_line_2, b.locality, b.mandal, b.city, b.district,
  b.state, 'IN', b.lat, b.lon, b.location_accuracy,
  b.opening_hours_raw, d.source_type, d.name, b.source_record_id, b.source_url,
  b.licence_name, b.attribution_text, b.confidence_score, b.completeness_score,
  b.freshness_score, b.tier::text, b.claim_status, b.verification_status,
  'directory_listing', true, true, true, true, b.last_seen_at,
  b.id
FROM businesses b
LEFT JOIN categories c   ON c.id = b.category_id
LEFT JOIN data_sources d ON d.id = b.source_id
LEFT JOIN pincodes p     ON p.code = b.pincode_code
WHERE {where}
  AND NOT ({excl})
ORDER BY b.pincode_code, c.slug, b.confidence_score DESC
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pincode", nargs="*")
    ap.add_argument("--city"); ap.add_argument("--district"); ap.add_argument("--state")
    ap.add_argument("--category", nargs="*")
    ap.add_argument("--tier", nargs="*", default=["CONTACTABLE", "LOCATABLE"])
    ap.add_argument("--min-confidence", type=int, default=0)
    ap.add_argument("--min-completeness", type=int, default=0)
    ap.add_argument("--format", choices=["csv", "json", "jsonl", "geojson"], default="csv")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--by", default="export_operator")
    a = ap.parse_args()

    where, args = build_where(a)
    excl_sql = " OR ".join(f"({s})" for _, s in EXCLUSIONS)
    conn = psycopg.connect(DSN)
    cur = conn.cursor()

    # what would be excluded, and why - reported, never silent
    reasons, excluded_total = {}, 0
    for name, clause in EXCLUSIONS:
        cur.execute(f"""SELECT count(*) FROM businesses b
                        LEFT JOIN categories c ON c.id=b.category_id
                        LEFT JOIN data_sources d ON d.id=b.source_id
                        LEFT JOIN pincodes p ON p.code=b.pincode_code
                        WHERE {where} AND ({clause})""", args)
        n = cur.fetchone()[0]
        if n:
            reasons[name] = n
    cur.execute(f"""SELECT count(*) FROM businesses b
                    LEFT JOIN categories c ON c.id=b.category_id
                    LEFT JOIN data_sources d ON d.id=b.source_id
                    LEFT JOIN pincodes p ON p.code=b.pincode_code
                    WHERE {where} AND ({excl_sql})""", args)
    excluded_total = cur.fetchone()[0]

    sql = SELECT.format(where=where, excl=excl_sql)
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    cur.execute(sql, args)
    rows = cur.fetchall()
    if not rows:
        print("no records match those filters (after exclusions)")
        print(f"excluded: {excluded_total:,} — {reasons}")
        return

    export_id = uuid.uuid4()
    d = OUTDIR / str(export_id)
    d.mkdir(parents=True, exist_ok=True)
    ext = {"csv": "csv", "json": "json", "jsonl": "jsonl", "geojson": "geojson"}[a.format]
    data_path = d / f"data.{ext}"

    recs = [dict(zip(FIELDS, r[:-1])) for r in rows]
    ids = [(export_id, r[-1], r[0]) for r in rows]

    if a.format == "csv":
        with open(data_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for x in recs:
                w.writerow(x)
    elif a.format == "jsonl":
        with open(data_path, "w", encoding="utf-8") as fh:
            for x in recs:
                fh.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
    elif a.format == "json":
        data_path.write_text(json.dumps(recs, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    else:                                     # geojson
        feats = []
        for x in recs:
            props = {k: v for k, v in x.items() if k not in ("latitude", "longitude")}
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point",
                                       "coordinates": [x["longitude"], x["latitude"]]},
                          "properties": props})
        data_path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                        ensure_ascii=False, default=str), encoding="utf-8")

    sha = hashlib.sha256(data_path.read_bytes()).hexdigest()

    # licence + attribution, aggregated from what actually shipped
    src = {}
    for x in recs:
        k = (x["source_name"], x["licence_name"], x["attribution_text"])
        src[k] = src.get(k, 0) + 1
    source_summary = [{"source": k[0], "licence": k[1], "attribution": k[2], "records": v}
                      for k, v in sorted(src.items(), key=lambda kv: -kv[1])]
    attribution = "\n".join(sorted({x["attribution_text"] for x in recs if x["attribution_text"]}))

    manifest = {
        "export_id": str(export_id),
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": a.by,
        "format": a.format,
        "filters": {k: v for k, v in vars(a).items() if v not in (None, [], 0)},
        "record_count": len(recs),
        "excluded_count": excluded_total,
        "exclusion_reasons": reasons,
        "source_summary": source_summary,
        "licence_summary": sorted({(x["licence_name"] or "unknown") for x in recs}),
        "attribution_requirements": attribution.split("\n") if attribution else [],
        "pincodes": sorted({x["pincode"] for x in recs if x["pincode"]}),
        "categories": sorted({x["category"] for x in recs if x["category"]}),
        "defaults_applied": {
            "claim_status": "unclaimed", "verification_status": "unverified",
            "publication_type": "directory_listing",
            "products_disabled": True, "offers_disabled": True,
            "reviews_disabled": True, "owner_contact_unconfirmed": True},
        "notes": [
            "Directory listings. Not owner-verified. Nothing inferred or fabricated.",
            "Absent fields are null and must stay absent in LocZ.",
            "location_accuracy governs display: 'locality' records must not show a distance.",
        ],
        "data_file": data_path.name,
        "sha256": sha,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    (d / "ATTRIBUTION.txt").write_text(attribution + "\n", encoding="utf-8")
    (d / "SHA256SUMS").write_text(f"{sha}  {data_path.name}\n", encoding="utf-8")

    cur.execute("""INSERT INTO exports (id,schema_version,format,filters,record_count,
                     excluded_count,exclusion_reasons,source_summary,licence_summary,
                     attribution,file_path,sha256,status,created_by,finished_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'complete',%s,now())""",
                (export_id, SCHEMA_VERSION, a.format, json.dumps(manifest["filters"], default=str),
                 len(recs), excluded_total, json.dumps(reasons),
                 json.dumps(source_summary), json.dumps(manifest["licence_summary"]),
                 attribution, str(data_path), sha, a.by))
    with cur.copy("COPY export_records (export_id,business_id,external_id) FROM STDIN") as cp:
        for row in ids:
            cp.write_row(row)
    conn.commit()

    print(f"export {export_id}")
    print(f"  records   {len(recs):,}")
    print(f"  excluded  {excluded_total:,}  {reasons}")
    print(f"  pincodes  {len(manifest['pincodes']):,}   categories {len(manifest['categories'])}")
    print(f"  sha256    {sha}")
    print(f"  path      {d}")
    for s in source_summary:
        print(f"    {s['source'][:44]:46s} {s['records']:7,}  {s['licence']}")
    conn.close()


if __name__ == "__main__":
    main()
