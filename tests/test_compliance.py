"""Compliance and data-integrity tests.

Every test here exists because something went wrong silently during development.
None of these failures raised an error at the time; all were caught only by reading
output. At 3.9M records that does not scale, so they are assertions now.

    python -m pytest tests/ -v          (or: python tests/test_compliance.py)
"""
import os
import re
import sys
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

# Sources whose terms forbid extraction. This list is code, not configuration:
# no admin action can add a row to data_sources that points at one of these.
DENYLIST = (
    "google.com", "maps.google", "business.google", "googleapis.com/maps",
    "justdial", "indiamart", "facebook", "instagram", "linkedin", "sulekha",
    "zomato", "swiggy", "magicbricks", "99acres", "housing.com", "nobroker",
    "tripadvisor", "yelp",
)

_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = psycopg.connect(DSN, connect_timeout=15)
    return _conn


def q(sql, args=None):
    cur = db().cursor()
    cur.execute(sql, args or ())
    return cur.fetchall()


def one(sql, args=None):
    return q(sql, args)[0][0]


# ---------------------------------------------------------------- source compliance
def test_no_denylisted_source():
    """A prohibited platform must never appear as a source, enabled or not."""
    rows = q("SELECT slug, COALESCE(base_url,'') FROM data_sources")
    bad = [(s, u) for s, u in rows if any(d in u.lower() or d in s.lower() for d in DENYLIST)]
    assert not bad, f"denylisted source registered: {bad}"


def test_enabled_sources_are_approved():
    """The DB constraint should make this impossible; assert it anyway."""
    n = one("""SELECT count(*) FROM data_sources WHERE enabled
               AND status NOT IN ('approved','approved_with_restrictions')""")
    assert n == 0, f"{n} enabled sources are not approved"


def test_every_source_has_a_licence():
    n = one("SELECT count(*) FROM data_sources WHERE enabled AND licence_name IS NULL")
    assert n == 0, f"{n} enabled sources have no recorded licence"


def test_redistributable_sources_carry_attribution():
    n = one("""SELECT count(*) FROM data_sources
               WHERE enabled AND redistribution_allowed AND attribution_text IS NULL""")
    assert n == 0, f"{n} redistributable sources have no attribution text"


# ---------------------------------------------------------------- provenance
def test_every_business_has_a_source():
    n = one("SELECT count(*) FROM businesses WHERE source_id IS NULL")
    assert n == 0, f"{n} businesses have no source"


def test_every_business_has_attribution():
    n = one("SELECT count(*) FROM businesses WHERE attribution_text IS NULL")
    assert n == 0, f"{n} businesses would export without attribution"


# ---------------------------------------------------------------- verification honesty
def test_no_import_claims_owner_verification():
    """An import may never assert more than source-level verification."""
    n = one("""SELECT count(*) FROM businesses
               WHERE verification_status NOT IN ('unverified','source_verified')""")
    assert n == 0, f"{n} imported records claim owner/manual verification"


def test_nothing_is_claimed_yet():
    n = one("SELECT count(*) FROM businesses WHERE claim_status <> 'unclaimed'")
    assert n == 0, f"{n} records claimed without an owner having claimed them"


# ---------------------------------------------------------------- geography
def test_coordinates_are_inside_india():
    """Caught a real defect: a name match once moved a pincode 2,659 km."""
    n = one("""SELECT count(*) FROM businesses
               WHERE lat NOT BETWEEN 6 AND 37.6 OR lon NOT BETWEEN 68 AND 97.5""")
    assert n == 0, f"{n} businesses sit outside India's bounding box"


def test_no_null_island():
    n = one("SELECT count(*) FROM businesses WHERE lat = 0 AND lon = 0")
    assert n == 0, f"{n} businesses at 0,0"


def test_corrected_centroids_moved_a_sane_distance():
    """Bounded correction: a pincode that 'moved' 100 km+ was matched to the
    wrong place. Village names repeat across India, so distance is the guard."""
    n = one("SELECT count(*) FROM pincodes WHERE centroid_offset_km > 100")
    assert n == 0, f"{n} pincodes were relocated more than 100 km"


def test_unverified_centroids_are_not_targetable():
    n = one("""SELECT count(*) FROM pincodes
               WHERE centroid_src = 'unverified' AND targetable""")
    assert n == 0, f"{n} pincodes are targetable despite an unverified centroid"


def test_nearest_pincode_assignment_is_not_used():
    """Removed deliberately: 69.4% of pincodes share a coordinate, so 'nearest'
    is a coin flip. Its absence is a rule, not an accident."""
    rows = q("""SELECT DISTINCT pincode_method::text FROM businesses
                WHERE pincode_method IS NOT NULL""")
    assert "nearest_pincode" not in [r[0] for r in rows], \
        "nearest_pincode assignment is in use"


# ---------------------------------------------------------------- phone integrity
def test_published_phones_are_well_formed():
    n = one(r"""SELECT count(*) FROM businesses
                WHERE public_phone IS NOT NULL
                  AND public_phone !~ '^\+91[1-9][0-9]{9}$'""")
    assert n == 0, f"{n} published phones are not valid Indian E.164"


def test_no_tollfree_published_as_a_shop_line():
    n = one(r"""SELECT count(*) FROM businesses
                WHERE public_phone ~ '^\+91(1800|1860|1900|140)'""")
    assert n == 0, f"{n} toll-free numbers published as a business's own line"


def test_no_number_shared_by_many_businesses():
    """Caught a real defect: one number appeared on 177 businesses across 20
    pincodes. Calling it reaches a call centre, not the shop."""
    worst = one("""SELECT COALESCE(max(c),0) FROM (
                     SELECT count(*) c FROM businesses
                     WHERE public_phone IS NOT NULL GROUP BY public_phone) x""")
    assert worst <= 3, f"one phone number is published on {worst} businesses"


def test_suppressed_phones_are_preserved_not_deleted():
    n = one("""SELECT count(*) FROM businesses
               WHERE phone_status IN ('shared_number','tollfree','malformed')
                 AND phone_raw IS NULL""")
    assert n == 0, f"{n} suppressed phones lost their original value"


# ---------------------------------------------------------------- fabrication
def test_no_fabricated_descriptions():
    """Sources supply no descriptions; if any exist, something invented them."""
    n = one("SELECT count(*) FROM businesses WHERE description IS NOT NULL")
    assert n == 0, f"{n} businesses have a description no source provided"


def test_implausible_opening_hours_rejected():
    """'sunrise-sunset' is valid OSM syntax and meaningless for a shop."""
    n = one("""SELECT count(*) FROM businesses
               WHERE lower(opening_hours_raw) IN ('sunrise-sunset','dawn-dusk','sunset-sunrise')""")
    assert n == 0, f"{n} records kept implausible opening hours"


# ---------------------------------------------------------------- export eligibility
def test_held_records_never_exported():
    n = one("""SELECT count(*) FROM export_records er
               JOIN businesses b ON b.id = er.business_id
               WHERE b.tier = 'HELD'""")
    assert n == 0, f"{n} HELD records were exported"


def test_exports_have_a_checksum():
    n = one("SELECT count(*) FROM exports WHERE status='complete' AND sha256 IS NULL")
    assert n == 0, f"{n} completed exports have no checksum"


def test_exports_record_attribution():
    n = one("""SELECT count(*) FROM exports
               WHERE status='complete' AND (attribution IS NULL OR attribution = '')""")
    assert n == 0, f"{n} exports carry no attribution block"


# ---------------------------------------------------------------- privacy
def test_no_personal_name_columns():
    """State registries ship an employer_name column. It must never be stored."""
    cols = [r[0] for r in q("""SELECT column_name FROM information_schema.columns
                               WHERE table_name = 'businesses'""")]
    banned = [c for c in cols if re.search(r"employer|proprietor|owner_name|aadhaar|pan_",
                                           c, re.I)]
    assert not banned, f"personal-data columns present: {banned}"


def test_leads_are_never_exported():
    """A lead is an internal outreach record, not a directory listing."""
    tables = [r[0] for r in q("""SELECT table_name FROM information_schema.tables
                                 WHERE table_schema='public'""")]
    if "business_leads" not in tables:
        return
    n = one("""SELECT count(*) FROM export_records er
               JOIN business_leads l ON l.business_id = er.business_id
               WHERE l.consent <> 'granted'""")
    assert n == 0, f"{n} unconsented leads reached an export"


# ---------------------------------------------------------------- runner
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {str(e)[:110]}")
            failed.append(name)
    print(f"\n{passed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
