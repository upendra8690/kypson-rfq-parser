"""
Randomised stress test - not part of the graded submission.

Generates thousands of synthetic RFQs from templates and checks parse_rfq against the labels
the generator knows by construction. Places are sampled from GeoNames Indian towns (plus some
foreign trade cities) that do NOT appear in my_tests.jsonl, so this measures how the parser
handles places, number formats and phrasings it was never tuned on.

It cannot measure realism: the templates encode my reading of the spec, so a high score here
means "robust to format variation", not "correct on real buyer messages".

    python stress_test.py            # 3000 messages, seed 7
    python stress_test.py 10000 42   # n, seed
"""
import json
import random
import sys
from collections import Counter

import geonamescache

from parse import parse_rfq

FIELDS = ("category", "quantity", "unit", "gsm", "micron", "location")

PRODUCTS = {
    "bopp_bag": ["bopp bags", "BOPP laminated bags", "bopp woven bag", "printed bopp bags", "bop bags",
                 "bopp laminated woven sacks", "BOPP bori"],
    "woven_sack": ["pp woven sacks", "woven sack", "hdpe bags", "cement bags", "AD star bags", "pp bags",
                   "pp bori", "woven polypropylene bags", "leno bags", "laminated woven sacks", "adstar bag"],
    "fibc": ["jumbo bags", "FIBC", "fibc bags", "bulk bags", "big bags", "jumbo bag", "jambo bag",
             "u panel fibc"],
    "film": ["shrink film", "stretch film", "shrink wrap", "stretch wrap", "pallet stretch film",
             "cling film", "LD shrink film", "BOPP film", "strech flim"],
    "corrugated_box": ["corrugated boxes", "5 ply boxes", "3 ply corrugated box", "boxes", "carton box",
                       "master cartons", "corugated box", "7 ply carton"],
    "mono_carton": ["mono cartons", "monocarton", "duplex boxes", "folding cartons", "mono carton box",
                    "printed mono cartons"],
    "pouch": ["stand up pouches", "zipper pouch", "pouches", "3 side seal pouch", "zip lock pouches",
              "laminated pouches", "spout pouch", "pouchs", "retort pouch"],
}
COUNT_FORMATS = [  # (template, value) - value given as a function of n
    (lambda n: f"{n}", 1), (lambda n: f"{n:,}", 1), (lambda n: _indian(n), 1),
]
COUNT_UNITS = ["pcs", "nos", "pieces", "units", "Nos.", "pc", ""]
QTY_CUES = ["", "", "qty ", "quantity: ", "Qty - ", "approx "]
HINGLISH = ["chahiye", "rate batao", "urgent", "pls quote", "need", "required", "", "", ""]
LOC_PATTERNS = ["{c}", "deliver to {c}", "delivery at {c}", "{c} mein", "location: {c}",
                "{c} delivery", "ship to {c}", "for {c} plant", "{c} tak bhejna hai", "at {c}"]
FOREIGN = ["Dubai", "Sharjah", "Abu Dhabi", "Muscat", "Doha", "Riyadh", "Jeddah", "Kuwait City",
           "Nairobi", "Mombasa", "Lagos", "Accra", "Kampala", "Dar es Salaam", "Kathmandu", "Dhaka",
           "Chittagong", "Colombo", "Singapore", "Kuala Lumpur", "Jakarta", "Bangkok", "Durban"]


def _indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + parts + [tail])


def _quantity_text(rng: random.Random):
    """Returns (text, value). Covers digits, Indian grouping, K, lakh/lac, hazaar, Hindi/English words."""
    choice = rng.random()
    if choice < 0.35:
        n = rng.choice([500, 750, 1000, 2000, 2500, 5000, 8000, 12000, 15000, 25000, 40000, 60000,
                        75000, 120000, 150000, 250000])
        fmt = rng.choice(COUNT_FORMATS)[0]
        return fmt(n), n
    if choice < 0.5:
        k = rng.choice([5, 10, 12, 15, 20, 25, 30, 40, 50, 75])
        return rng.choice([f"{k}k", f"{k}K", f"{k} k", f"{k} thousand", f"{k} hazaar", f"{k} hazar"]), k * 1000
    if choice < 0.7:
        v = rng.choice([1, 1.5, 2, 2.5, 3, 5, 10, 1.25])
        word = rng.choice(["lakh", "lac", "lakhs", "lacs", "L"]) if v != 1.25 else "lakh"
        txt = f"{v:g} {word}" if word != "L" else f"{v:g}L"
        return txt, int(v * 100000)
    if choice < 0.85:
        return rng.choice([("dedh lakh", 150000), ("dhai lakh", 250000), ("sawa lakh", 125000),
                           ("do lakh", 200000), ("teen lakh", 300000), ("paanch hazaar", 5000),
                           ("das hazaar", 10000), ("bees hazaar", 20000), ("pachaas hazaar", 50000),
                           ("ek lakh", 100000), ("saade teen lakh", 350000)])
    return rng.choice([("fifty thousand", 50000), ("twenty five thousand", 25000), ("one lakh", 100000),
                       ("ten thousand", 10000), ("two lakh fifty thousand", 250000),
                       ("2 lakh 50 thousand", 250000), ("1 lakh 20 hazaar", 120000)])


def _make(rng: random.Random, places: list) -> dict:
    cat = rng.choice(list(PRODUCTS))
    product = rng.choice(PRODUCTS[cat])
    exp = {"category": cat, "quantity": None, "unit": None, "gsm": None, "micron": None, "location": None}
    parts = [product]

    # order quantity
    if cat == "film" and rng.random() < 0.8:
        v, unit = rng.choice([(500, "kg"), (800, "kg"), (1000, "kg"), (2, "mt"), (1.5, "mt"), (5, "mt"),
                              (20, "mt")])
        word = rng.choice(["kg", "kgs"]) if unit == "kg" else rng.choice(["ton", "tons", "MT", "tonnes", "mt"])
        parts.append(f"{rng.choice(QTY_CUES)}{v:g} {word}")
        exp["quantity"], exp["unit"] = v, unit
    elif cat == "woven_sack" and rng.random() < 0.15:
        v = rng.choice([5, 10, 20, 30])
        parts.append(f"{v} {rng.choice(['ton', 'MT', 'tons'])}")
        exp["quantity"], exp["unit"] = v, "mt"
    elif rng.random() < 0.92:
        txt, v = _quantity_text(rng)
        unit_word = rng.choice(COUNT_UNITS) if not txt.endswith("L") else "pcs"
        cue = rng.choice(QTY_CUES)
        parts.append(f"{cue}{txt} {unit_word}".strip())
        exp["quantity"], exp["unit"] = v, ("kg" if cat == "film" and not unit_word else "pcs")

    # specs + distractors that must NOT become the quantity
    if cat in ("bopp_bag", "woven_sack") and rng.random() < 0.7:
        g = rng.choice([55, 60, 65, 70, 75, 80, 90])
        parts.append(rng.choice([f"{g} gsm", f"{g}gsm", f"GSM {g}", f"{g} GSM"]))
        exp["gsm"] = g
    if cat in ("bopp_bag", "woven_sack") and rng.random() < 0.6:
        parts.append(rng.choice(["25 kg", "50kg", "25 kg capacity", "50 kg wale", "for 25 kg rice",
                                 "10 kg", "18x30 inch", "3 colour printing", "Rs 8 per bag"]))
    if cat == "fibc" and rng.random() < 0.8:
        parts.append(rng.choice(["1000 kg capacity", "1 ton", "1.5 MT SWL", "1000kg", "500 kg", "4 loop",
                                 "2 ton capacity", "1000 kg SWL"]))
    if cat == "fibc" and rng.random() < 0.3:
        g = rng.choice([140, 160, 180, 200])
        parts.append(f"{g} gsm fabric")
        exp["gsm"] = g
    if cat in ("film", "pouch") and rng.random() < 0.7:
        m = rng.choice([12, 18, 20, 23, 25, 40, 50, 75, 90, 100])
        parts.append(rng.choice([f"{m} micron", f"{m} mic", f"{m}mic", f"{m} microns", f"{m} µ"]))
        exp["micron"] = m
    if cat == "pouch" and rng.random() < 0.5:
        parts.append(rng.choice(["500 gm", "1 kg", "250g", "500 ml", "200 gm wale", "12 x 18 cm"]))
    if cat == "corrugated_box" and rng.random() < 0.5:
        parts.append(rng.choice(["5 ply", "12x10x8 inch", "BF 18", "3 ply", "size 400x300x200 mm"]))
    if cat == "mono_carton" and rng.random() < 0.6:
        g = rng.choice([250, 280, 300, 350, 400])
        parts.append(rng.choice([f"{g} gsm", f"{g}gsm FBB", f"{g} gsm duplex"]))
        exp["gsm"] = g

    # location
    if rng.random() < 0.8:
        city = rng.choice(places)
        parts.append(rng.choice(LOC_PATTERNS).format(c=city))
        exp["location"] = city

    head, rest = parts[0], parts[1:]
    rng.shuffle(rest)
    filler = rng.choice(HINGLISH)
    sep = rng.choice([", ", " ", " - ", ", "])
    text = sep.join([head] + rest)
    if filler:
        text = f"{text} {filler}" if rng.random() < 0.5 else f"{filler} {text}"
    case = rng.random()
    if case < 0.25:
        text = text.lower()
    elif case < 0.35:
        text = text.upper()
    return {"text": text, "expected": exp}


def _same_place(a, b) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def main(n: int = 3000, seed: int = 7) -> None:
    rng = random.Random(seed)
    seen = set()
    for line in open("my_tests.jsonl", encoding="utf-8"):
        loc = json.loads(line)["expected"]["location"]
        if loc:
            seen.add(loc.lower())
    gc = geonamescache.GeonamesCache(min_city_population=15000)
    places = sorted({c["name"] for c in gc.get_cities().values()
                     if c["countrycode"] == "IN" and c["population"] >= 50_000 and c["name"].isascii()
                     and " " not in c["name"] and c["name"].lower() not in seen})
    places += [f for f in FOREIGN if f.lower() not in seen]
    rows = [_make(rng, places) for _ in range(n)]
    ok = Counter()
    exact = 0
    errors = []
    for row in rows:
        got, exp = parse_rfq(row["text"]), row["expected"]
        wrong = []
        for f in FIELDS:
            good = _same_place(got[f], exp[f]) or got[f] == exp[f] if f == "location" else got[f] == exp[f]
            ok[f] += good
            if not good:
                wrong.append(f)
        exact += not wrong
        if wrong:
            errors.append((row["text"], {f: (exp[f], got[f]) for f in wrong}))
    print(f"{n} synthetic messages, {len(places)} unseen places (location compared case-insensitively)")
    for f in FIELDS:
        print(f"  {f:<9} {100 * ok[f] / n:5.1f}%")
    print(f"  exact     {100 * exact / n:5.1f}%")
    by_field = Counter(f for _, d in errors for f in d)
    print("errors by field:", dict(by_field))
    for text, diff in errors[:40]:
        print(" ", text, "|", diff)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:3]))
