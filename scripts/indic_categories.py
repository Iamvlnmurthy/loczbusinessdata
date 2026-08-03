"""Categorise businesses whose names are in Indian scripts.

Every name rule written so far is Latin-only, so a shop called
`శ్రీ శివశక్తి జ్యోతిష్యాలయం` (an astrology centre) fell through to whatever the
source tag said — in that case grocery-and-kirana, which is how an astrologer
ended up in a kirana search.

The blind spot is 41,336 records, about 1% of the directory. Small, but visibly
wrong to any user who reads the script, and concentrated in exactly the local
businesses this project exists to surface.

Terms are the trade words that appear on real shop boards, not transliterations of
English marketing copy. Devanagari and Telugu are covered most thoroughly because
they are the largest group and the launch state respectively.
"""
import os, sys
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]


def _dsn():
    v = os.environ.get("LOCZ_DSN")
    if not v:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("LOCZ_DSN="):
                return line.split("=", 1)[1].strip()
        raise SystemExit("LOCZ_DSN is not set")
    return v


# (regex alternation, category slug, subcategory, business_type)
RULES = [
    # --- astrology / priestly services: the case that started this ---
    (r"ज्योतिष|ज्योतिषी|జ్యోతిష్య|జ్యోతిష్యాలయ|ஜோதிட|ಜ್ಯೋತಿಷ",
     "professional-services", "astrologer", "SERVICE_PROVIDER"),

    # --- medical ---
    (r"मेडिकल|मेडिकल्स|दवाखाना|औषध|फार्मेसी|మెడికల్|మెడికల్స్|ఔషధ|"
     r"மருந்தக|ಮೆಡಿಕಲ್|മെഡിക്കൽ|ওষুধ|મેડિકલ",
     "medical-stores-and-pharmacies", "chemist", "RETAIL_STORE"),
    (r"अस्पताल|चिकित्सालय|क्लिनिक|दवाखाना|ఆసుపత్రి|ఆస్పత్రి|క్లినిక్|"
     r"மருத்துவமனை|ಆಸ್ಪತ್ರೆ|ആശുപത്രി|হাসপাতাল",
     "hospitals-and-clinics", "clinic", "SERVICE_PROVIDER"),

    # --- food ---
    (r"होटल|रेस्टोरेंट|ढाबा|भोजनालय|मेस|హోటల్|రెస్టారెంట్|భోజన|మెస్|"
     r"உணவகம்|ஹோட்டல்|ಹೋಟೆಲ್|ഹോട്ടൽ|হোটেল|રેસ્ટોરન્ટ",
     "restaurants-and-food", "restaurant", "FOOD_SERVICE"),
    (r"बेकरी|मिठाई|स्वीट्स|బేకరీ|స్వీట్|మిఠాయి|பேக்கரி|ಬೇಕರಿ|ബേക്കറി|মিষ্টি",
     "bakeries-and-sweets", "sweets", "FOOD_SERVICE"),

    # --- retail ---
    (r"किराना|जनरल स्टोर|परचून|राशन|కిరాణా|జనరల్ స్టోర్|"
     r"மளிகை|ಕಿರಾಣಿ|പലചരക്ക്|মুদি",
     "grocery-and-kirana", "kirana-store", "RETAIL_STORE"),
    (r"कपड़ा|वस्त्र|साड़ी|रेडीमेड|బట్టల|వస్త్ర|చీరల|துணி|ಬಟ್ಟೆ|തുണി|কাপড়",
     "clothing-stores", "clothing-store", "RETAIL_STORE"),
    (r"ज्वेलर्स|ज्वैलर्स|सोनार|జ్యువెల్లర్|నగల|ஜவுளி|ನಗ|ജ്വല്ലറി|গহনা",
     "jewellery-stores", "jewellery-store", "RETAIL_STORE"),
    (r"हार्डवेयर|लोहा|सीमेंट|హార్డ్‌వేర్|ఇనుము|சிமெண்ட்|ಹಾರ್ಡ್‌ವೇರ್",
     "hardware-stores", "hardware-store", "RETAIL_STORE"),
    (r"इलेक्ट्रॉनिक|इलेक्ट्रिक|ఎలక్ట్రిక్|ఎలక్ట్రానిక్|மின்|ಎಲೆಕ್ಟ್ರಿಕ್",
     "electronics-stores", "electronics-store", "RETAIL_STORE"),
    (r"मोबाइल|మొబైల్|மொபைல்|ಮೊಬೈಲ್|മൊബൈൽ|মোবাইল",
     "mobile-stores", "mobile-phone-store", "RETAIL_STORE"),
    (r"फर्नीचर|ఫర్నిచర్|மரச்சாமான்|ಪೀಠೋಪಕರಣ|ഫർണിച്ചർ",
     "furniture-stores", "furniture-store", "RETAIL_STORE"),

    # --- services ---
    (r"सैलून|ब्यूटी|पार्लर|సెలూన్|బ్యూటీ|పార్లర్|அழகு|ಸಲೂನ್|ബ്യൂട്ടി",
     "salons-and-spas", "salon", "SERVICE_PROVIDER"),
    (r"दर्जी|टेलर|సైదర్|టైలర్|தையல்|ಟೈಲರ್|തയ്യൽ|দর্জি",
     "tailoring-and-boutiques", "tailor", "SERVICE_PROVIDER"),
    (r"गैरेज|मोटर्स|वर्कशॉप|గ్యారేజ్|మోటార్స్|ಗ್ಯಾರೇಜ್",
     "car-repair", "car-service", "SERVICE_PROVIDER"),
    (r"विद्यालय|पाठशाला|स्कूल|పాఠశాల|విద్యాలయ|స్కూల్|"
     r"பள்ளி|ಶಾಲೆ|സ്കൂൾ|বিদ্যালয়",
     "schools", "school", "INSTITUTION"),
    (r"मंडप|टेंट|शादी|కళ్యాణ|టెంట్|మండప|மண்டப|ಮಂಟಪ",
     "event-services", "tent-house", "SERVICE_PROVIDER"),
    (r"पेट्रोल पंप|पेट्रोल|పెట్రోల్|பெட்ரோல்|ಪೆಟ್ರೋಲ್",
     "petrol-stations", "petrol-station", "RETAIL_STORE"),
]


def main():
    conn = psycopg.connect(_dsn(), autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout='30min'")
    cur.execute("SELECT slug, id FROM categories")
    cat = dict(cur.fetchall())

    total = 0
    print(f"{'category':32s} {'subcategory':20s} moved")
    for rx, slug, sub, btype in RULES:
        if slug not in cat:
            print(f"  !! unknown category {slug}")
            continue
        cur.execute("""UPDATE businesses
                       SET category_id = %s, subcategory = %s, business_type = %s,
                           updated_at = now()
                       WHERE display_name ~ %s
                         AND category_id IS DISTINCT FROM %s""",
                    (cat[slug], sub, btype, rx, cat[slug]))
        if cur.rowcount:
            total += cur.rowcount
            print(f"  {slug:32s} {sub:20s} {cur.rowcount:6,}")   # regex omitted: console is cp1252
    print(f"\nrecategorised {total:,}")

    cur.execute("""SELECT display_name, c.slug FROM businesses b
                   JOIN categories c ON c.id = b.category_id
                   WHERE b.subcategory = 'astrologer' LIMIT 5""")
    rows = cur.fetchall()
    if rows:
        print("\nastrologers now correctly filed:")
        for n, s in rows:
            print(f"  {s:26s} {n[:44].encode("ascii","replace").decode()}")
    conn.close()


if __name__ == "__main__":
    main()
