"""Final corrections that keyword rules cannot express.

NOTE: Postgres POSIX regex uses \y for a word boundary.  means backspace, so
every rule below silently matched nothing until this was fixed.

Three fixes, each from a specific observation rather than a general rule:

  1. Tent houses are event-rental businesses (tents, chairs, wedding decor).
     No international taxonomy has a word for them, so a name rule is the only
     honest way to catch them. They were landing in hotels, then professional
     services - both wrong.

  2. Banks, ATMs and post offices pass "the public can transact here" but fail
     "someone could claim and maintain this listing". A national bank will not
     manage 50,000 branch pages, and a claim button on an SBI branch is noise.
     They stay findable, but as PUBLIC_SERVICE so they never inflate merchant
     counts or enter the acquisition funnel.

  3. Names that are only a category word ("Medical", "Hotel", "General Store")
     carry no identity. They are flagged, not deleted - a real shop may simply
     be signed that way - but they should not be the first result a user sees.
"""
import os
from pathlib import Path
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

NAME_RULES = [
    # (regex, category slug, subcategory, business_type)
    (r"\y(tent house|tent works|tents? (and|&) )", "event-services", "tent-house", "SERVICE_PROVIDER"),
    (r"\y(decorat(ors?|ion)|mandap|shamiana)\y", "event-services", "decorators", "SERVICE_PROVIDER"),
    (r"\y(function hall|kalyana ?mandap|convention)\y", "event-services", "function-hall", "HOSPITALITY"),
    (r"\y(xerox|photostat)\y", "printing-and-stationery", "photocopy", "SERVICE_PROVIDER"),
    (r"\y(tiffin ?cent|mess)\y", "restaurants-and-food", "tiffin-centre", "FOOD_SERVICE"),
    (r"\y(kirana|provision ?stores?)\y", "grocery-and-kirana", "kirana-store", "RETAIL_STORE"),
    (r"\y(pan ?shop|paan ?shop)\y", "grocery-and-kirana", "pan-shop", "RETAIL_STORE"),
]

UTILITY_CATEGORIES = ("professional-services",)
UTILITY_NAME = (r"\y(state bank|sbi|hdfc bank|icici bank|axis bank|canara bank|"
                r"union bank|bank of (baroda|india|maharashtra)|punjab national|"
                r"indian bank|central bank of india|uco bank|idbi|kotak mahindra bank|"
                r"post ?office|atm)\y")

GENERIC_NAMES = (r"^(medical|medicals|hotel|restaurant|general ?store|kirana|"
                 r"shop|store|bakery|salon|clinic|hospital|school|pharmacy|"
                 r"super ?market|hardware|electricals?|mobiles?)$")


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout='60min'")
    cur.execute("""ALTER TABLE businesses
                     ADD COLUMN IF NOT EXISTS quality_flags text[]""")
    cur.execute("SELECT slug, id FROM categories")
    cat = dict(cur.fetchall())

    print("1. name-based category corrections")
    for rx, slug, sub, bt in NAME_RULES:
        if slug not in cat:
            print(f"   !! unknown category {slug}"); continue
        cur.execute("""UPDATE businesses SET category_id=%s, subcategory=%s,
                              business_type=%s, updated_at=now()
                       WHERE display_name ~* %s AND category_id IS DISTINCT FROM %s""",
                    (cat[slug], sub, bt, rx, cat[slug]))
        if cur.rowcount:
            print(f"   {rx[:38]:40s} -> {slug:24s} {cur.rowcount:7,}")

    print("\n2. utilities marked PUBLIC_SERVICE (findable, not claimable)")
    cur.execute("""UPDATE businesses SET business_type='PUBLIC_SERVICE', updated_at=now()
                   WHERE display_name ~* %s AND business_type IS DISTINCT FROM 'PUBLIC_SERVICE'""",
                (UTILITY_NAME,))
    print(f"   banks / ATMs / post offices : {cur.rowcount:,}")

    print("\n3. flagging low-identity names (kept, ranked down)")
    cur.execute("""UPDATE businesses
                   SET quality_flags = array_append(COALESCE(quality_flags,'{}'), 'generic_name')
                   WHERE display_name ~* %s
                     AND NOT COALESCE(quality_flags,'{}') @> ARRAY['generic_name']""",
                (GENERIC_NAMES,))
    print(f"   name is only a category word : {cur.rowcount:,}")

    cur.execute("""UPDATE businesses
                   SET quality_flags = array_append(COALESCE(quality_flags,'{}'), 'no_contact_no_address')
                   WHERE public_phone IS NULL AND address_line_1 IS NULL
                     AND website IS NULL
                     AND NOT COALESCE(quality_flags,'{}') @> ARRAY['no_contact_no_address']""")
    print(f"   no phone, address or website : {cur.rowcount:,}")

    print("\n--- result ---")
    cur.execute("""SELECT COALESCE(business_type,'(none)'), count(*) FROM businesses
                   GROUP BY 1 ORDER BY 2 DESC""")
    for bt, n in cur.fetchall():
        print(f"  {bt:20s} {n:9,}")
    cur.execute("""SELECT unnest(quality_flags) f, count(*) FROM businesses
                   WHERE quality_flags IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    print()
    for f, n in cur.fetchall():
        print(f"  flag {f:26s} {n:9,}")
    cur.execute("SELECT display_name, subcategory FROM businesses WHERE subcategory='tent-house' LIMIT 3")
    print()
    for a, b in cur.fetchall():
        print(f"  check: {a[:44]:46s} -> {b}")
    conn.close()


if __name__ == "__main__":
    main()
