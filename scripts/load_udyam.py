"""UDYAM MSME register (all India) via the data.gov.in open-data API.

42.5 million registered micro, small and medium enterprises, published by the
Ministry of MSME. Name, pincode, street address, district, state, registration
date, and declared activities as NIC 5-digit codes.

WHY THIS IS NOT LOADED AS LISTINGS
    No coordinates. Same rule as the Telangana registers: a record that cannot be
    placed cannot be deduplicated by distance, and name-only matching is what moved
    a pincode 2,659 km earlier in this project. These land in `registry_entries`.

    But unlike Telangana, these carry a PINCODE — which is this engine's primary
    key. That makes them far more useful: matching is name + pincode rather than
    name + district, and gap analysis works at the unit we actually track.

PRIVACY
    `CommunicationAddress` for a micro-enterprise is frequently the proprietor's
    home. Rows whose address looks residential are flagged `home_address_suspected`
    and must not be published without consent. No proprietor name is present in the
    feed, which is why this is usable at all.

Licence: data.gov.in / NDSAP — Government Open Data Licence India. Attribution
required. Verify the catalogue page before redistributing anything derived from it.
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = "8b68ae56-84cf-4728-a0a6-1be11028dea7"
API = f"https://api.data.gov.in/resource/{RESOURCE}"
UA = "LocZ-Pincode-Business-Engine/0.1 (infovivencia2026@gmail.com)"
PAGE = 10_000


def _env(name):
    v = os.environ.get(name)
    if v:
        return v
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} is not set")


DSN = _env("LOCZ_DSN")
KEY = _env("DATA_GOV_KEY")

# a communication address that reads residential rather than commercial
HOME_HINTS = re.compile(
    r"\b(h\.?no|house\s*no|door\s*no|flat|apartment|apt|residency|residence|"
    r"nagar\s+colony|colony|quarters|villa|behind\s+house)\b", re.I)


def clean(v, n=None):
    if v is None:
        return None
    v = re.sub(r"\s+", " ", "".join(c for c in str(v) if ord(c) >= 32)).strip()
    return (v[:n] if n else v) or None


def canon(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    s = re.sub(r"\b(the|and|m s|ms|sri|shri|new|pvt|ltd|private|limited|"
               r"enterprises?|enterprise|traders?|trading)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def activities(raw):
    """NIC codes are the state's own description of what the business does —
    better evidence than any tag we could infer."""
    if not raw:
        return None, None
    try:
        acts = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None, None
    if not isinstance(acts, list) or not acts:
        return None, None
    first = acts[0] or {}
    return clean(first.get("Description"), 200), clean(first.get("NIC5DigitId"), 8)


def fetch(offset, retries=4):
    q = urllib.parse.urlencode({"api-key": KEY, "format": "json",
                                "limit": PAGE, "offset": offset})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r).get("records") or []
        except Exception as exc:
            if attempt == retries - 1:
                print(f"    offset {offset:,} failed: {type(exc).__name__}", flush=True)
                return None
            time.sleep(5 * (attempt + 1))     # back off, do not hammer
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-records", type=int, default=0, help="0 = all")
    ap.add_argument("--states", nargs="*", help="keep only these states")
    a = ap.parse_args()

    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS registry_entries (
                     id bigserial PRIMARY KEY, registry text NOT NULL,
                     source_file text NOT NULL, name text NOT NULL,
                     canonical_name text NOT NULL, nature text, category_hint text,
                     district text, mandal text, village text, city text,
                     pincode_code varchar(6), address text, phone text,
                     registered_on text, status text, extra jsonb,
                     matched_business_id uuid, match_method text,
                     match_score numeric(3,2),
                     created_at timestamptz NOT NULL DEFAULT now())""")
    cur.execute("""INSERT INTO data_sources
        (slug,name,source_type,adapter_key,base_url,provider_name,licence_name,
         attribution_text,commercial_use_allowed,storage_allowed,redistribution_allowed,
         automated_access_allowed,enabled,status,reviewed_by,reviewed_at,notes)
        VALUES ('udyam-msme','UDYAM MSME Register (data.gov.in)','government',
                'datagov_api','https://api.data.gov.in',
                'Ministry of Micro, Small and Medium Enterprises','GODL-India',
                'Ministry of MSME, Government of India — data.gov.in',
                true,true,true,true,true,'approved','phase-3',now(),
                'Documented open-data API with a registered key. No coordinates: '
                'verification and gap analysis only, never published as listings. '
                'Communication addresses are often residential — flagged, not published.')
        ON CONFLICT (slug) DO UPDATE SET updated_at=now()""")
    conn.commit()

    cur.execute("SELECT count(*) FROM registry_entries WHERE registry='udyam'")
    already = cur.fetchone()[0]
    offset = (already // PAGE) * PAGE
    print(f"already loaded : {already:,}   resuming at offset {offset:,}", flush=True)

    total, homes, t0 = already, 0, time.time()
    limit = a.max_records or 42_531_970
    states = {s.upper() for s in (a.states or [])}

    while total < limit:
        recs = fetch(offset)
        if recs is None:
            print("  giving up after retries", flush=True)
            break
        if not recs:
            print("  end of dataset", flush=True)
            break

        batch = []
        for r in recs:
            name = clean(r.get("EnterpriseName"), 200)
            if not name:
                continue
            state = clean(r.get("State"), 80)
            if states and (state or "").upper() not in states:
                continue
            pin = clean(r.get("Pincode"))
            if pin:
                pin = re.sub(r"\.0$", "", pin)
                pin = pin if re.fullmatch(r"\d{6}", pin) else None
            addr = clean(r.get("CommunicationAddress"), 300)
            desc, nic = activities(r.get("Activities"))
            is_home = bool(addr and HOME_HINTS.search(addr))
            homes += is_home
            batch.append((
                "udyam", f"udyam_offset_{offset}", name, canon(name)[:200],
                desc, nic, clean(r.get("District"), 80), None, None, None,
                pin, addr, None, clean(r.get("RegistrationDate"), 20),
                "registered",
                json.dumps({"state": state, "nic": nic,
                            "home_address_suspected": is_home}, default=str)))

        if batch:
            with cur.copy("""COPY registry_entries (registry, source_file, name,
                canonical_name, nature, category_hint, district, mandal, village,
                city, pincode_code, address, phone, registered_on, status, extra)
                FROM STDIN""") as cp:
                for row in batch:
                    cp.write_row(row)
            conn.commit()
        total += len(batch)
        offset += PAGE

        if (offset // PAGE) % 20 == 0:
            el = time.time() - t0
            rate = (total - already) / max(el, 1)
            eta = (limit - total) / max(rate, 1) / 60
            print(f"  {total:,} loaded · {rate:,.0f}/s · ETA {eta:,.0f} min", flush=True)
        time.sleep(0.15)                       # courtesy to a public API

    el = (time.time() - t0) / 60
    print(f"\nloaded {total:,} UDYAM records in {el:.1f} min")
    print(f"residential-looking addresses flagged: {homes:,}")

    cur.execute("""SELECT count(*), count(pincode_code), count(address),
                          count(DISTINCT district)
                   FROM registry_entries WHERE registry='udyam'""")
    n, p, ad, d = cur.fetchone()
    print(f"  with pincode  {p:,} / {n:,}\n  with address  {ad:,}\n  districts     {d:,}")
    conn.close()


if __name__ == "__main__":
    main()
