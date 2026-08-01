"""Build the merchant-acquisition queue from licensed directory data.

  python scripts/build_leads.py --district Hyderabad --limit 5000

No scraping involved: leads come from businesses already in the engine, which came
from OpenStreetMap (ODbL) and Overture (CDLA-Permissive). Both permit storage and
reuse, so contacting a business whose phone it publishes is ordinary outreach.

A lead is internal. It is never exported to LocZ. Only a merchant who consents and
onboards becomes a LocZ profile, authored by them.
"""
import argparse, json, os
import psycopg

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

# Categories that solve the cold-start problem first: product-led, high search
# intent, and a merchant who benefits obviously from being findable.
TIER1 = ["hardware-stores", "electrical-stores", "furniture-stores", "mobile-stores",
         "computer-and-laptop-stores", "pet-stores-and-pet-services",
         "grocery-and-kirana", "bakeries-and-sweets", "gift-stores",
         "tailoring-and-boutiques", "electronics-stores", "medical-stores-and-pharmacies"]
TIER2 = ["plumbing-services", "electrical-services", "repair-services", "home-services",
         "cleaning-services", "salons-and-spas", "beauty-and-wellness",
         "tuition-and-coaching", "courier-and-parcel-services", "car-repair", "bike-repair"]

SCORE_SQL = """
WITH cat AS (SELECT id, slug FROM categories)
SELECT b.id, b.external_id, COALESCE(b.resolved_name,b.display_name), b.category_id,
       b.pincode_code, b.locality, b.district, b.state,
       b.public_phone, b.phone_line_type, b.public_email, b.website, b.lat, b.lon,
       c.slug,
       -- score components, kept explicit so the breakdown can be stored
       (CASE WHEN b.public_phone IS NOT NULL THEN 20 ELSE 0 END)                    AS s_phone,
       (CASE WHEN b.phone_line_type = 'mobile' THEN 10 ELSE 0 END)                  AS s_mobile,
       (CASE WHEN b.website IS NOT NULL OR b.public_email IS NOT NULL THEN 10 ELSE 0 END) AS s_web,
       (CASE WHEN c.slug = ANY(%s) THEN 25 WHEN c.slug = ANY(%s) THEN 15 ELSE 5 END) AS s_cat,
       (CASE WHEN b.pincode_confidence >= 0.90 THEN 20
             WHEN b.pincode_confidence >= 0.70 THEN 10 ELSE 0 END)                  AS s_loc,
       (CASE WHEN b.location_accuracy = 'building' THEN 10 ELSE 5 END)              AS s_geo,
       (CASE WHEN b.freshness_score >= 50 THEN 10
             WHEN b.freshness_score IS NULL THEN 0 ELSE 3 END)                      AS s_fresh,
       COALESCE(g.gap,0)                                                            AS gap
FROM businesses b
JOIN cat c ON c.id = b.category_id
LEFT JOIN coverage_gaps g ON g.pincode_code = b.pincode_code AND g.category_id = b.category_id
LEFT JOIN business_leads l ON l.external_id = b.external_id
LEFT JOIN contact_suppression s ON s.phone = b.public_phone
WHERE l.id IS NULL
  AND s.phone IS NULL
  AND b.tier <> 'HELD'
  AND b.pincode_code IS NOT NULL
  AND {extra}
ORDER BY (CASE WHEN b.public_phone IS NOT NULL THEN 20 ELSE 0 END
        + CASE WHEN c.slug = ANY(%s) THEN 25 WHEN c.slug = ANY(%s) THEN 15 ELSE 5 END
        + CASE WHEN b.pincode_confidence >= 0.90 THEN 20 ELSE 0 END) DESC
LIMIT %s
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--district"); ap.add_argument("--state"); ap.add_argument("--city")
    ap.add_argument("--pincode", nargs="*")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--with-phone-only", action="store_true")
    a = ap.parse_args()

    extra, args = ["1=1"], []
    if a.district:
        extra.append("b.district ILIKE %s"); args.append(a.district)
    if a.state:
        extra.append("b.state ILIKE %s"); args.append(a.state)
    if a.city:
        extra.append("b.city ILIKE %s"); args.append(a.city)
    if a.pincode:
        extra.append("b.pincode_code = ANY(%s)"); args.append(a.pincode)
    if a.with_phone_only:
        extra.append("b.public_phone IS NOT NULL")

    sql = SCORE_SQL.format(extra=" AND ".join(extra))
    params = [TIER1, TIER2] + args + [TIER1, TIER2, a.limit]

    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    print(f"candidates: {len(rows):,}")
    if not rows:
        return

    inserted = 0
    with cur.copy("""COPY business_leads
        (business_id, external_id, name, category_id, pincode_code, locality, district,
         state, contact_phone, phone_line_type, contact_email, website, lat, lon,
         score, score_breakdown, status) FROM STDIN""") as cp:
        for r in rows:
            (bid, ext, name, cid, pin, loc, dist, st, phone, line, mail, site, lat, lon,
             slug, s_phone, s_mobile, s_web, s_cat, s_loc, s_geo, s_fresh, gap) = r
            s_gap = min(gap * 5, 25)
            total = s_phone + s_mobile + s_web + s_cat + s_loc + s_geo + s_fresh + s_gap
            breakdown = {"phone": s_phone, "mobile_line": s_mobile, "web_or_email": s_web,
                         "category_priority": s_cat, "location_confidence": s_loc,
                         "geometry": s_geo, "freshness": s_fresh, "coverage_gap": s_gap,
                         "total": total}
            status = "ready_for_outreach" if phone else "incomplete"
            cp.write_row((bid, ext, name, cid, pin, loc, dist, st, phone, line, mail,
                          site, lat, lon, total, json.dumps(breakdown), status))
            inserted += 1
    conn.commit()
    print(f"leads created: {inserted:,}")

    cur.execute("""SELECT status::text, count(*), round(avg(score)) FROM business_leads
                   GROUP BY 1 ORDER BY 2 DESC""")
    print("\nby status:")
    for s, n, avg in cur.fetchall():
        print(f"  {s:20s} {n:8,}   avg score {avg}")

    cur.execute("""SELECT c.slug, count(*), round(avg(l.score))
                   FROM business_leads l JOIN categories c ON c.id=l.category_id
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")
    print("\ntop lead categories:")
    for s, n, avg in cur.fetchall():
        print(f"  {s:34s} {n:7,}   avg {avg}")

    cur.execute("""SELECT name, locality, pincode_code, contact_phone, score
                   FROM business_leads WHERE status='ready_for_outreach'
                   ORDER BY score DESC LIMIT 8""")
    print("\nhighest-priority outreach targets:")
    for n, loc, pin, ph, sc in cur.fetchall():
        print(f"  [{sc:3d}] {n[:42]:44s} {str(loc)[:16]:18s} {pin}  {ph}")
    conn.close()


if __name__ == "__main__":
    main()
