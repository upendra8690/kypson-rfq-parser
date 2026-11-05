"""
parse.py - turn a short, messy B2B packaging RFQ into structured fields.

    >>> parse_rfq("bopp bags chahiye 40 hazaar, 65 gsm, Nagpur")
    {'category': 'bopp_bag', 'quantity': 40000, 'unit': 'pcs', 'gsm': 65, 'micron': None, 'location': 'Nagpur'}

Approach (deterministic rules, no ML, no network):

  1. _normalise      unicode + glued tokens ("65gsm" -> "65 gsm", "Rs.8" -> "rs 8", "µ" -> "micron")
  2. _tokenise       words / numbers / punctuation; original casing is kept for locations
  3. _fix_typo       alias table + edit distance against the product vocabulary ("corugated", "flim")
  4. _read_numbers   merge number expressions into Num items: "1,50,000", "2.5 lac", "15K",
                     "40 hazaar", "dedh lakh", "sawa lakh", "fifty thousand", "2 lakh 50 thousand"
  5. _label_numbers  give every Num a role from its neighbours: gsm, micron, size, ply, price,
                     date, pincode, weight (kg / mt), count, or bare
  6. _category       weighted keyword rules (specific phrases beat generic words)
  7. _pick_quantity  drop fill weights / capacities ("25 kg bag", "1000 kg capacity"), then take
                     cued qty > explicit count > order weight > bare number
  8. _location       GeoNames gazetteer (cities, alternate names, countries, Indian states)
                     scored by cue words ("deliver to", "... mein", trailing ", City"),
                     with a capitalised-word fallback for places not in the gazetteer

Run from the shell:  python parse.py "jumbo bag 1000 kg capacity, 500 pcs, Vadodara"
           trace:   python parse.py --explain "jumbo bag 1000 kg capacity, 500 pcs, Vadodara"
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Union

CATEGORIES = ("bopp_bag", "woven_sack", "corrugated_box", "pouch", "film", "mono_carton", "fibc")


def _w(s: str) -> frozenset:
    return frozenset(s.split())


# ---------------------------------------------------------------------------
# Units and context words
# ---------------------------------------------------------------------------
KG_UNITS = _w("kg kgs kilo kilos kilogram kilograms kgms")
MT_UNITS = _w("mt mts ton tons tonne tonnes tonn tn tns")
QUINTAL_UNITS = _w("quintal quintals qtl qtls qntl")
SMALL_WEIGHT_UNITS = _w("g gm gms gram grams gramme grm grms gr")
VOLUME_UNITS = _w("ml ltr ltrs litre litres liter liters lit lt l")
GSM_UNITS = _w("gsm gsms grammage")
MICRON_UNITS = _w("mic mics micron microns micro mcn mu um microne")
GAUGE_UNITS = _w("gauge gage guage")
DIM_UNITS = _w("inch inches cm cms mm mtr mtrs meter meters metre metres m ft feet foot \" '")
COUNT_UNITS = _w(
    "pcs pc piece pieces pce nos no number numbers unit units nag nang pkt pkts "
    "set sets roll rolls bundle bundles bale bales sheet sheets "
    "bag bags sack sacks box boxes carton cartons pouch pouches packet packets "
    "bori boriya boriyan katta katte dabba dabbe peti fibc fibcs jumbo"
)
# a number followed by one of these is a count of items ("5000 printed bopp bags")
PRODUCT_WORDS = COUNT_UNITS | _w(
    "bopp woven pp hdpe ldpe lldpe ld jumbo corrugated mono monocarton monocartons duplex "
    "shrink stretch cling zipper zip ziplock standup stand laminated printed plain unprinted "
    "white brown transparent kraft cement ad adstar valve leno bulk big film films wrap "
    "retort spout doypack sachet sachets master shipping multicolour virgin heavy fresh new "
    "polypropylene poly polythene plastic"
)
CONTAINER_WORDS = _w(
    "bag bags sack sacks bori boriya boriyan katta katte pouch pouches box boxes carton cartons "
    "fibc jumbo packet packets roll rolls"
)
QTY_CUES = _w("qty qnty qnt qtty quantity quantities requirement requirements reqmt order moq volume total")
SAMPLE_CUES = _w("sample samples trial test testing demo")
FILLERS = _w(": - = is of approx approximately appx aprox around about ~ upto min minimum lagbhag nearly least")
ORDINALS = _w("st nd rd th")
MONTHS = _w(
    "jan january feb february mar march apr april may jun june jul july aug august "
    "sep sept september oct october nov november dec december"
)
EXCL_AFTER = _w(
    "ply plys layer layers lyr colour colours color colors clr clrs col side sided sides "
    "denier den mesh bf ect loop loops panel panels flute fluted "
    "day days din week weeks wk wks hafta hafte month months mahina mahine "
    "hr hrs hour hours ghante ghanta mins minute minutes year years yr yrs saal "
    "% percent pct rs rupees rupee inr /- paise deg degree degrees times kgcm2 psi "
    "truck trucks lorry lorries trolley container containers fcl load loads vehicle vehicles gaadi"
)
# unit-like words that label the number *after* them ("BF 18", "ply: 5") - only when free-standing
UNIT_BEFORE = _w("bf ect denier den mesh ply colour color colours loops loop panel layer layers")
PRICE_BEFORE = _w("rs inr rate rates price prices budget cost mrp @ within phone mob mobile ph contact call "
                  "whatsapp pin pincode gst gstin no # ref id invoice")
# label the number after them as a size - unless a count unit follows ("width 50 rolls" is still 50 rolls)
SIZE_BEFORE = _w("size sizes sz dimension dimensions length width height dia diameter thickness")
PER_PRICE_UNITS = _w("kg kgs pc pcs piece pieces bag bags unit units nos no box boxes roll rolls sack sacks pouch pouches carton cartons sqm sqft meter mtr")
FILL_CUES_STRONG = _w("capacity cap swl load loading filling fill holding each")
FILL_CUES_WEAK = _w("packing pack wala wale wali waala waale waali ka ke ki weight wt")
LIQUID_HINTS = _w("pouch pouches bottle bottles jar jars can cans jerrycan drum drums bucket buckets oil ghee water milk liquid juice tin tins spout")

# max plausible fill weight / capacity (kg) for one item of each category
FILL_MAX_KG = {"bopp_bag": 75, "woven_sack": 100, "pouch": 25, "corrugated_box": 50,
               "mono_carton": 10, "fibc": 3000, "film": 30, None: 100}

# ---------------------------------------------------------------------------
# Number words (English + Hindi/Hinglish)
# ---------------------------------------------------------------------------
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "paach": 5,
    "chhe": 6, "chhah": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "barah": 12, "baarah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20, "pachees": 25,
    "pachis": 25, "pachchis": 25, "tees": 30, "chalis": 40, "chaalis": 40, "chalees": 40,
    "pachas": 50, "pachaas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
}
# words that are also ordinary English/Hindi words: only numbers when a multiplier follows
AMBIGUOUS_NUMBER_WORDS = _w("do che nau das dus bees saath tees char")
FRACTION_WORDS = {"dedh": 1.5, "dhed": 1.5, "derh": 1.5, "dhedh": 1.5, "dhai": 2.5, "dhaai": 2.5,
                  "adhai": 2.5, "arhai": 2.5, "half": 0.5, "aadha": 0.5, "adha": 0.5}
MODIFIER_WORDS = {"sawa": 0.25, "sava": 0.25, "savaa": 0.25, "sawaa": 0.25, "saade": 0.5,
                  "sade": 0.5, "saadhe": 0.5, "sadhe": 0.5, "paune": -0.25, "pone": -0.25}
MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3, "thousands": 1e3, "thou": 1e3, "hazaar": 1e3, "hazar": 1e3,
    "hajar": 1e3, "hajaar": 1e3, "hazzar": 1e3, "hzr": 1e3, "hazaron": 1e3,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "laakh": 1e5, "lkh": 1e5, "lacks": 1e5,
    "crore": 1e7, "crores": 1e7, "cr": 1e7, "crs": 1e7, "karod": 1e7, "karor": 1e7,
    "million": 1e6, "millions": 1e6, "mn": 1e6,
    "hundred": 100, "sau": 100, "dozen": 12,
}

# ---------------------------------------------------------------------------
# Typos
# ---------------------------------------------------------------------------
TYPO_ALIASES = {
    "bop": "bopp", "boop": "bopp", "bpp": "bopp", "bopps": "bopp",
    "flim": "film", "flims": "films", "filim": "film", "fillm": "film",
    "fibcs": "fibc", "fibic": "fibc", "fbic": "fibc", "fibcc": "fibc",
    "jambo": "jumbo", "jumboo": "jumbo", "jumbu": "jumbo", "jumbos": "jumbo",
    "cartoon": "carton", "cartoons": "cartons", "cartan": "carton", "cartun": "carton",
    "boxs": "boxes", "boxe": "box", "bxs": "boxes",
    "pouchs": "pouches", "pauch": "pouch", "pouche": "pouch", "poch": "pouch", "pouc": "pouch",
    "sak": "sack", "saks": "sacks", "sacs": "sacks",
    "srink": "shrink", "shrnk": "shrink", "shrik": "shrink", "shrinck": "shrink",
    "strech": "stretch", "streach": "stretch", "stech": "stretch",
    "wovan": "woven", "wooven": "woven", "wovn": "woven",
    "corr": "corrugated", "corru": "corrugated", "corrugate": "corrugated",
    "peices": "pieces", "pices": "pieces", "pcss": "pcs",
    "qunatity": "quantity", "quantiy": "quantity", "quntity": "quantity", "qauntity": "quantity",
    "micorn": "micron", "mircon": "micron", "microm": "micron",
    "chaiye": "chahiye", "chahie": "chahiye", "zipock": "ziplock",
}
FUZZY_TARGETS = (
    "woven", "sacks", "corrugated", "carton", "cartons", "pouch", "pouches", "jumbo", "shrink",
    "stretch", "laminated", "lamination", "monocarton", "duplex", "cement", "zipper", "standup",
    "pieces", "quantity", "capacity", "micron", "microns", "delivery", "polypropylene", "printed",
    "hazaar", "thousand", "boxes", "bulk", "retort",
)

# ---------------------------------------------------------------------------
# Stop words: never a place, never typo-corrected
# ---------------------------------------------------------------------------
STOPWORDS = _w(
    # English
    "a an the and or of for to in at on by from with without is are am be been was were will "
    "shall should would can could may might must need needs needed want wants wanted require "
    "required requires requirement looking look buy buying purchase quote quotation quotes rate "
    "rates price prices best good quality urgent urgently asap immediately please pls plz kindly "
    "send share give provide supply supplier suppliers buyer we i our my me you your us they it "
    "this that these those have has had hi hello dear sir madam team regards thanks thank "
    "ok okay yes no not any some all also only just very more less per each every month monthly "
    "weekly week day days year yearly annual order orders enquiry inquiry details detail "
    "size sizes type types standard custom customized customised brand logo front back side top "
    "bottom open mouth handle liner inner outer coated uncoated virgin recycled natural clear "
    "milky black red blue green yellow brown golden silver metallic matt matte gloss glossy "
    "white plain print printing printed colour color colors colours multicolour multi heavy "
    "duty light export import local material materials packaging packing package packages pack "
    "packs packet packets fill fills filling filled women men punch porch couch rolls roll "
    "food grade rice wheat atta maida flour sugar salt dal pulses cement sand fertilizer "
    "fertiliser feed cattle poultry fish chemical chemicals powder granules seeds seed grain "
    "grains tea coffee spices masala namkeen snacks chips biscuits pharma medicine medicines "
    "cosmetic cosmetics garments garment shoes shoe electronics ecommerce e commerce fmcg dairy "
    "milk ghee oil pet petfood frozen meat sweets sweet mithai chocolate toys toy agro "
    "fabric board kraft paper mix mixed new fresh delivered delivery deliver location loc city "
    "site address ship shipping dispatch despatch destination dest port place area plant "
    "factory unit warehouse godown depot based near nr approx around about minimum maximum "
    "max min total sample samples trial test testing demo stock available ready same next "
    "lowest final confirm confirmed rush wrapping wrap sealing seal sealed lock zip "
    "stand up spout window shape round square flat gusset gusseted bottom block valve "
    "machine auto automatic manual roll stock sleeve sleeves label labels tape sheet sheets "
    "cover covers liner liners bopp pp hdpe ldpe lldpe pet pe cpp pvc pof ld hm "
    "pallet pallets lamination laminated laminate fbb sbs duplex mono rsc "
    "x by upto till before after within soon today tomorrow call contact whatsapp number mobile "
    "pin pincode code phone mob email gst gstin id ref invoice width length height thickness dia "
    "company firm traders trader industries industry enterprises pvt ltd limited group sons brothers "
    "agency agencies corporation private works mills mill foods products product shipment shipments "
    "consignment payment advance credit cash bill freight transport truck trucks container load "
    # Hinglish
    "chahiye chaiye chahie chahte chahta chahti chahiyen hai hain ho hoga hogi tha thi ka ke ki "
    "ko se me mein mai par pe tak aur ya bhi wala wale wali waala waale waali jaldi abhi turant "
    "daam bhav kitna kitne kya kab kaha kahan bhejo bhejna bhej bhejiye dena dedo de karo karna "
    "karein chalega sahi achha accha acha sasta saste mehenga maal saman samaan liye lie sath "
    "naya nayi purana pakka kacha lagbhag bhai ji sab har mahina mahine din hafte saal "
    "mujhe humein hume hamein hamko mera meri hamara hamari apna apni aapka aap please "
    "bori boriya boriyan katta katte dabba dabbe peti thaili thaila panni gatta nag nang "
    "rakhna batao bataye bataiye kijiye dijiye milega milegi"
)


# ---------------------------------------------------------------------------
# Gazetteer (cities, countries, Indian states)
# ---------------------------------------------------------------------------
INDIAN_STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Orissa", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Jammu and Kashmir", "Kashmir", "Ladakh", "Puducherry", "Pondicherry",
    "Chandigarh", "Delhi", "Dadra and Nagar Haveli", "Daman and Diu", "Lakshadweep",
    "Andaman and Nicobar",
)
COUNTRY_ALIASES = {
    "uae": "UAE", "usa": "USA", "uk": "UK", "ksa": "KSA", "saudi": "Saudi", "saudi arabia": "Saudi Arabia",
    "emirates": "Emirates", "america": "America", "england": "England", "britain": "Britain",
    "sri lanka": "Sri Lanka", "srilanka": "Sri Lanka", "bangladesh": "Bangladesh",
    "nepal": "Nepal", "bhutan": "Bhutan", "ncr": "NCR",
}
UPPER_NAMES = _w("uae usa uk ksa ncr")
# Always loaded; also keeps the parser usable if geonamescache is missing (much smaller coverage).
BUILTIN_CITIES = (
    "Mumbai Delhi Bengaluru Bangalore Hyderabad Ahmedabad Chennai Kolkata Surat Pune Jaipur Lucknow "
    "Kanpur Nagpur Indore Thane Bhopal Visakhapatnam Vizag Patna Vadodara Baroda Ghaziabad Ludhiana "
    "Agra Nashik Faridabad Meerut Rajkot Varanasi Srinagar Aurangabad Dhanbad Amritsar Allahabad "
    "Prayagraj Ranchi Howrah Coimbatore Jabalpur Gwalior Vijayawada Jodhpur Madurai Raipur Kota "
    "Guwahati Chandigarh Solapur Hubli Hubballi Bareilly Moradabad Mysore Mysuru Gurgaon Gurugram "
    "Aligarh Jalandhar Tiruchirappalli Bhubaneswar Salem Warangal Thiruvananthapuram Trivandrum "
    "Bhiwandi Saharanpur Gorakhpur Guntur Bikaner Amravati Noida Jamshedpur Bhilai Cuttack "
    "Firozabad Kochi Cochin Bhavnagar Dehradun Durgapur Asansol Nanded Kolhapur Ajmer Gulbarga "
    "Jamnagar Ujjain Siliguri Jhansi Jammu Mangalore Mangaluru Erode Belgaum Belagavi Tirunelveli "
    "Gaya Jalgaon Udaipur Tirupur Tiruppur Davanagere Kozhikode Calicut Akola Kurnool Bokaro "
    "Rajahmundry Ballari Bellary Bilaspur Panipat Karnal Bathinda Rampur Shimla Haridwar Rudrapur "
    "Roorkee Hosur Vapi Valsad Navsari Bharuch Ankleshwar Anand Mehsana Gandhinagar Morbi Junagadh "
    "Silvassa Daman Baddi Sonipat Rohtak Hisar Ambala Panchkula Mohali Bhiwadi Alwar Bhilwara "
    "Sikar Pali Satna Rewa Sagar Dewas Pithampur Ratlam Haldia Kharagpur Rourkela Sambalpur "
    "Puducherry Vellore Thrissur Kollam Kannur Palakkad Sivakasi Thoothukudi Tuticorin Nellore "
    "Tirupati Kakinada Karimnagar Nizamabad Latur Satara Sangli Ahmednagar Chakan Sanand Halol "
    "Kathmandu Dhaka Colombo Dubai Sharjah Riyadh Jeddah Doha Muscat Nairobi Lagos Mombasa "
    "Singapore London Johannesburg Karachi Kabul Kuwait-City"
).split()


def _fold(s: str) -> str:
    """ASCII-fold, lowercase, collapse separators: 'Hosūr' -> 'hosur', 'Navi-Mumbai' -> 'navi mumbai'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`\-_/]", " ", s)
    return " ".join(s.split())


@lru_cache(maxsize=1)
def _gazetteer() -> dict:
    """key -> (display, kind, population, in_india, is_main_name). kind: city | country | state."""
    gaz: dict = {}
    rank = {"state": 0, "country": 1, "city": 2}

    def add(name: str, kind: str, pop: int, display: Optional[str] = None, india: bool = False,
            main: bool = True) -> None:
        key = _fold(name)
        if len(key) < 3 or not re.fullmatch(r"[a-z]+(?: [a-z]+){0,3}", key):
            return
        disp = display or unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        old = gaz.get(key)
        if old is None or (rank[kind], pop, main) > (rank[old[1]], old[2], old[4]):
            gaz[key] = (disp, kind, pop, india, main)

    for name in BUILTIN_CITIES:
        add(name, "city", 100_000, india=True)
    for name in INDIAN_STATES:
        add(name, "state", 0, india=True)
    for alias, disp in COUNTRY_ALIASES.items():
        add(alias, "country", 1_000_000, disp)
    try:
        import geonamescache  # optional but recommended: ~6.5k Indian towns + world cities
    except Exception:  # pragma: no cover - fallback path
        return gaz
    gc = geonamescache.GeonamesCache(min_city_population=5000)
    for c in gc.get_countries().values():
        add(c["name"], "country", int(c.get("population") or 0))
    for c in gc.get_cities().values():
        pop, india = int(c.get("population") or 0), c.get("countrycode") == "IN"
        if not india and pop < 100_000:
            continue
        add(c["name"], "city", pop, india=india)
        if (india and pop >= 20_000) or pop >= 1_000_000:
            for alt in c.get("alternatenames") or ():
                if alt and alt.isascii() and len(alt) >= 4 and alt[0].isupper() and not alt.isupper():
                    add(alt, "city", pop, india=india, main=False)
    return gaz


# Place names that are also ordinary words. Found by intersecting the gazetteer with the top 40k
# English words (wordfreq) and a Hinglish word list, then hand-picking the ordinary words (real
# city names such as London or Washington were left out of the list).
# Hard-blocked words count as a place only when Capitalised right after a cue ("deliver to Split").
GAZ_BLOCK = _w(
    "than got man bad run along nice bank reading bed san bar male drug ran mobile surprise bay ray "
    "split orange hang kim independence hat bush bath rail enterprise pen begun ann commonwealth punch "
    "ali oral springs dig bare metro jam paradise tours delta hub abu pal alot karma saga salon ant mon "
    "manor providence shaping vista gill tar mango kara brits ala vita weir barking tyre chu moth corona "
    "diva bender centennial manu mascara canning raja sakura nadia lau billings tal uri cypress rees "
    "taping woodlands ara kira ras patti nag samba ruse gent lander toto yao nada wai bah kant bora "
    "tundra nagar raman mage sari aron agar bello chia lira manger rani kari lala shankar duma naga "
    "kaya mohan dali itu nuh wail bimbo ica simmering suri bela raya moro tama wadi pail pare shakti "
    "medea rona bata mera bada kot krishna swl bag bags box boxes film best star plain gold food bulk mix "
    "print printed white roll rolls sale side need rate"
)
# Real Indian towns that are also everyday words/names: accepted when Capitalised, after a cue,
# or as the last word of the message ("..., Mandi").
_SOFT_BLOCK = _w("mandi bari pali una mau rath dhar gaya banda sachin hassan puri anand bela chas")


@lru_cache(maxsize=1)
def _fuzzy_city_index() -> dict:
    """first letter -> [(key, display, is_main_name, pop)] for Indian cities >= 100k, for typo matching."""
    idx: dict = {}
    for key, (disp, kind, pop, india, main) in _gazetteer().items():
        if india and kind == "city" and pop >= 100_000 and len(key) >= 5 and " " not in key:
            idx.setdefault(key[0], []).append((key, disp, main, pop))
    return idx


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@dataclass
class Tok:
    text: str               # lowercase, typo-corrected
    orig: str               # as written
    kind: str               # word | num | punct
    value: Optional[float] = None


@dataclass
class Num:
    value: float
    raw: str
    has_mult: bool
    wordy: bool
    top_mult: float = 0.0
    role: str = ""          # gsm micron kg mt count bare fill small dim price date pin phone other range_hi
    kg: Optional[float] = None
    micron: Optional[float] = None
    cue: bool = False
    sample: bool = False


VOCAB = (STOPWORDS | PRODUCT_WORDS | KG_UNITS | MT_UNITS | QUINTAL_UNITS | SMALL_WEIGHT_UNITS
         | VOLUME_UNITS | GSM_UNITS | MICRON_UNITS | GAUGE_UNITS | DIM_UNITS | EXCL_AFTER
         | QTY_CUES | SAMPLE_CUES | MONTHS | frozenset(MULTIPLIERS) | frozenset(NUMBER_WORDS)
         | frozenset(FRACTION_WORDS) | frozenset(MODIFIER_WORDS) | frozenset(TYPO_ALIASES)
         | frozenset(TYPO_ALIASES.values()) | frozenset(FUZZY_TARGETS) | FILL_CUES_STRONG
         | FILL_CUES_WEAK | CONTAINER_WORDS | UNIT_BEFORE | SIZE_BEFORE | PRICE_BEFORE | FILLERS
         | _w("fibc jumbo sack sacks woven corrugated pouch pouches carton cartons monocarton "
              "shrink stretch cling ad star adstar leno gunny raffia doypack sachet retort "
              "zipper ziplock standup bopp"))

_TOKEN_RE = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?|\.\d+|[^\W\d_]+(?:['-][^\W\d_]+)*|/-|\S", re.UNICODE)
_INDIAN_GROUPS = re.compile(r"\d{1,2}(?:,\d{2})*,\d{3}(?:\.\d+)?")
_WESTERN_GROUPS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = re.sub("[\u00b5\u03bc\u039c]", " micron ", t)        # µ, Greek mu, and its upper case
    t = (t.replace("×", " x ").replace("₹", " rs ").replace("–", "-")
          .replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"'))
    t = re.sub(r"(?i)\bgm?s?\s*/\s*(?:sq\.?\s*)?m(?:tr|eter|etre)?\s*2\b", " gsm ", t)  # g/m2
    t = re.sub(r"(?i)kg\s*/\s*cm\s*2", " kgcm2 ", t)                                     # burst strength
    t = re.sub(r"(?i)\b(?:[a-z]\.){2,}[a-z]?\b", lambda m: m.group(0).replace(".", ""), t)  # u.a.e. m.t.
    t = re.sub(r"(?i)\brs\.?(?=\s*\d)", " rs ", t)
    t = re.sub(r"(?<=[^\W\d_])-(?=\d)|(?<=\d)-(?=[^\W\d_])", " ", t)  # 3-ply, qty-5000
    t = re.sub(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)", " ", t)     # 65gsm, 40k, 2.5lac, 12x10
    return t


def _tokenise(text: str) -> list:
    toks: list = []
    for m in _TOKEN_RE.finditer(text):
        s = m.group(0)
        if s[0].isdigit() or (s[0] == "." and len(s) > 1):
            if "," in s and not (_INDIAN_GROUPS.fullmatch(s) or _WESTERN_GROUPS.fullmatch(s)):
                parts = s.split(",")          # "40000,65" is two numbers, not one
                for k, p in enumerate(parts):
                    if k:
                        toks.append(Tok(",", ",", "punct"))
                    if p:
                        toks.append(Tok(p, p, "num", float(p)))
                continue
            toks.append(Tok(s, s, "num", float(s.replace(",", ""))))
        elif s[0].isalpha():
            if "-" in s and _fold(s) not in _gazetteer():      # "stand-up" -> stand - up,
                for k, part in enumerate(s.split("-")):         # but keep "Pimpri-Chinchwad" whole
                    if k:
                        toks.append(Tok("-", "-", "punct"))
                    toks.append(Tok(part.lower(), part, "word"))
                continue
            toks.append(Tok(s.lower(), s, "word"))
        else:
            toks.append(Tok(s, s, "punct"))
    return toks


def _osa(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment edit distance (insert/delete/substitute/transpose), early exit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _fix_typo(word: str) -> str:
    if word in TYPO_ALIASES:
        return TYPO_ALIASES[word]
    if len(word) < 5 or not word.isalpha() or not word.isascii() or word in VOCAB or word in _gazetteer():
        return word
    best, best_d = word, 99
    for target in FUZZY_TARGETS:
        limit = 1 if len(target) <= 7 else 2
        d = _osa(word, target, limit)
        if d <= limit and d < best_d:
            best, best_d = target, d
    return best


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def _multiplier(toks: list, j: int, cur: Optional[float], cur_word: bool) -> float:
    w = toks[j].text
    if w == "k":                                   # "20k", "20 K" - only straight after digits
        return 1e3 if cur is not None and not cur_word else 0.0
    if w == "l":                                   # "1.5L pcs" = lakh, "2L pouch" = litre
        if cur is None or cur_word or cur >= 100:
            return 0.0
        nxt = toks[j + 1].text if j + 1 < len(toks) else ""
        return 0.0 if nxt in LIQUID_HINTS else 1e5
    return MULTIPLIERS.get(w, 0.0)


def _read_number(toks: list, i: int, ceiling: float = math.inf):
    """Read one number expression starting at toks[i]. Returns (Num, next_index) or None."""
    n = len(toks)
    total, cur, cur_word, mod = 0.0, None, False, None
    used_digit = used_mult = False
    top = 0.0
    raw_parts = []
    j = i
    while j < n:
        t = toks[j]
        w = t.text
        if t.kind == "num":
            if cur is not None:
                break
            cur, cur_word, used_digit = t.value, False, True
            if mod is not None:
                cur, mod = cur + mod, None
        elif t.kind != "word":
            break
        elif w in NUMBER_WORDS:
            v = float(NUMBER_WORDS[w])
            if cur is None:
                cur, cur_word = v, True
                if mod is not None:
                    cur, mod = cur + mod, None
            elif cur_word and 20 <= cur < 100 and cur % 10 == 0 and v < 10:
                cur += v                              # "twenty five"
            else:
                break
        elif w in FRACTION_WORDS and cur is None and mod is None:
            cur, cur_word = FRACTION_WORDS[w], True
        elif w in MODIFIER_WORDS and cur is None and mod is None:
            mod = MODIFIER_WORDS[w]
        elif w in ("a", "an") and j == i and j + 1 < n and toks[j + 1].text in MULTIPLIERS and toks[j + 1].text != "k":
            cur, cur_word = 1.0, True
        else:
            m = _multiplier(toks, j, cur, cur_word)
            if not m:
                break
            if cur is None:
                if mod is None:
                    break
                cur, mod = 1.0 + mod, None            # "sawa lakh" = 1.25 lakh
            raw_parts.append(t.orig)
            used_mult, top = True, max(top, m)
            if m in (100.0, 12.0):                    # hundred / sau / dozen scale the current group
                cur *= m
                j += 1
                continue
            if m >= ceiling:
                raw_parts.pop()
                break
            total += cur * m
            cur = None
            j += 1
            k = j + 1 if j < n and toks[j].text in ("and", "&", "+") else j
            if k < n:                                 # "2 lakh 50 thousand", "teen hazaar paanch sau"
                sub = _read_number(toks, k, ceiling=m)
                if sub and sub[0].has_mult and sub[0].top_mult < m:
                    total += sub[0].value
                    raw_parts.append(sub[0].raw)
                    j = sub[1]
            break
        raw_parts.append(t.orig)
        j += 1
    if j == i or (cur is None and not used_mult):
        return None
    value = total + (cur or 0.0)
    wordy = not used_digit
    if wordy and not used_mult:
        first = toks[i].text
        nxt = toks[j].text if j < n else ""
        unitish = (nxt in COUNT_UNITS or nxt in PRODUCT_WORDS or nxt in KG_UNITS or nxt in MT_UNITS
                   or nxt in GSM_UNITS or nxt in MICRON_UNITS)    # "twenty five micron"
        if first in AMBIGUOUS_NUMBER_WORDS or not unitish:
            return None
    return Num(value, " ".join(raw_parts), used_mult, wordy, top), j


def _read_numbers(toks: list) -> list:
    items: list = []
    i = 0
    while i < len(toks):
        res = _read_number(toks, i) if toks[i].kind in ("num", "word") else None
        if res is None:
            items.append(toks[i])
            i += 1
        else:
            items.append(res[0])
            i = res[1]
    return items


NUM = "\x00num"   # what _txt returns for a number item


def _txt(items: list, k: int) -> str:
    if 0 <= k < len(items):
        it = items[k]
        return NUM if isinstance(it, Num) else it.text
    return ""


def _prev_meaningful(items: list, i: int, count: int = 2) -> list:
    """Up to `count` (index, word) pairs before i, skipping filler words like ':' 'approx'."""
    out = []
    k = i - 1
    while k >= 0 and len(out) < count:
        w = _txt(items, k)
        if w in FILLERS:
            k -= 1
            continue
        if w in (",", ";", "|", "(", ")", "\n", "."):
            break
        out.append((k, w))
        k -= 1
    return out


# Plausible value range for a number that a spec word attaches to. Used to decide which number a
# unit belongs to: in "65 gsm 40000" gsm belongs to 65; in "2,000 GSM 75" it belongs to 75.
UNIT_RANGE = {**{u: (8, 600) for u in GSM_UNITS}, **{u: (3, 500) for u in MICRON_UNITS},
              "bf": (8, 60), "ect": (1, 30), "ply": (1, 9), "colour": (1, 12), "color": (1, 12),
              "colours": (1, 12), "denier": (100, 3000), "den": (100, 3000), "mesh": (1, 40),
              "loop": (1, 8), "loops": (1, 8), "panel": (1, 8), "layer": (1, 11), "layers": (1, 11)}


def _plausible(word: str, value: float) -> bool:
    lo, hi = UNIT_RANGE.get(word, (0, math.inf))
    return lo <= value <= hi


def _free(items: list, k: int) -> bool:
    """True if the spec word at k is not the unit of the number just before it ('65 gsm 40000')."""
    if k < 1 or not isinstance(items[k - 1], Num):
        return True
    before = items[k - 1]
    return before.has_mult or not _plausible(items[k].text, before.value)


def _label(items: list, i: int, end: int) -> None:
    n: Num = items[i]
    v = n.value
    a1, a2 = _txt(items, end), _txt(items, end + 1)
    b1 = _txt(items, i - 1)
    prev = _prev_meaningful(items, i, 3)
    digits = re.sub(r"\D", "", n.raw) if not n.wordy else ""

    def set_role(role: str) -> None:
        n.role = role

    # identifiers, dates
    if digits and not n.has_mult:
        prev_phone = i >= 1 and isinstance(items[i - 1], Num) and items[i - 1].role == "phone"
        if len(digits) >= 10 or b1 == "+" or (len(digits) == 5 and prev_phone):
            return set_role("phone")
        if (len(digits) == 6 and v % 1000 and digits[0] in "12345678"
                and a1 not in COUNT_UNITS and a1 not in KG_UNITS and a1 not in MT_UNITS):
            return set_role("pin")
    if a1 in ORDINALS or a1 in MONTHS or b1 in MONTHS:
        return set_role("date")
    # specs
    if a1 in GSM_UNITS and _plausible(a1, v):
        return set_role("gsm")
    if a1 in MICRON_UNITS and _plausible(a1, v):
        n.micron = v
        return set_role("micron")
    if a1 in GAUGE_UNITS:
        n.micron = v * 0.254                       # 100 gauge = 25.4 micron
        return set_role("micron")
    if a1 == "mm" and v < 1:
        n.micron = v * 1000                        # 0.05 mm = 50 micron
        return set_role("micron")
    for k, w in prev[:1]:
        if w in GSM_UNITS and _free(items, k) and a1 not in KG_UNITS | MT_UNITS | COUNT_UNITS:
            return set_role("gsm")
        if w in MICRON_UNITS and _free(items, k) and a1 not in KG_UNITS | MT_UNITS | COUNT_UNITS:
            n.micron = v
            return set_role("micron")
    # weights
    cue = bool(prev) and prev[0][1] in QTY_CUES     # "qty 5000", "Quantity: approx 5000"
    n.cue = cue
    n.sample = any(w in SAMPLE_CUES for _, w in prev)
    if a1 in KG_UNITS:
        n.kg = v
        return set_role("kg")
    if a1 in MT_UNITS or (a1 == "metric" and a2 in MT_UNITS):
        n.kg = v * 1000
        return set_role("mt")
    if a1 in QUINTAL_UNITS:
        n.value, n.kg = v * 100, v * 100
        return set_role("kg")
    if a1 in SMALL_WEIGHT_UNITS or a1 in VOLUME_UNITS:
        return set_role("small")                    # "250 gm pouch", "500 ml"
    # sizes
    partner_weight = a2 == NUM and _txt(items, end + 2) in KG_UNITS | MT_UNITS
    if a1 in ("x", "*", "by") and a2 == NUM and not partner_weight:
        return set_role("dim")
    if b1 in ("x", "*", "by") and _txt(items, i - 2) == NUM and a1 not in KG_UNITS | MT_UNITS:
        return set_role("dim")
    if a1 in DIM_UNITS:
        return set_role("dim")
    # everything else that is not an order quantity
    if (a1 in EXCL_AFTER and not (a1 in ("rs", "inr", "rupees") and a2 == NUM)
            and not (a1 in UNIT_RANGE and (n.has_mult or not _plausible(a1, v)))):
        return set_role("other")    # "50 rs", "5 ply" - but not "75 hazaar Rs 8" or "5 lakh BF 18"
    if b1 == "@" or any(w in PRICE_BEFORE for _, w in prev[:1]):
        return set_role("price")
    for k, w in prev[:1]:
        if (w in UNIT_BEFORE or w in SIZE_BEFORE) and _free(items, k) and a1 not in PRODUCT_WORDS:
            return set_role("other")            # "BF 18", "width 450" - but not "u panel 300 nos"
    if a1 in ("per", "/") and a2 in PER_PRICE_UNITS:
        return set_role("price")                    # "12 per kg"
    if a1 in ("qty", "quantity", "qnty"):
        n.cue = True                                # "20k qty"
        return set_role("count")
    if a1 in COUNT_UNITS or a1 in PRODUCT_WORDS:
        return set_role("count")
    return set_role("bare")


def _label_numbers(items: list) -> list:
    nums = []
    for i, it in enumerate(items):
        if not isinstance(it, Num) or it.role:
            continue
        end = i + 1
        hi = items[i + 2] if i + 2 < len(items) else None
        if _txt(items, i + 1) in ("-", "to", "~") and isinstance(hi, Num) and it.value < hi.value <= it.value * 10:
            hi.role = "range_hi"                    # "60-70 gsm": use the low end, unit from the high end
            end = i + 3
        _label(items, i, end)
        nums.append((i, end, it))
    return nums


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------
_BAGS = r"(?:bags?|sacks?|bori|boriya|katt[ae])"
CATEGORY_RULES = [  # (category, weight, pattern) - higher weight wins, ties go to the earliest match
    ("fibc", 10, r"\bfibc\b|\bjumbo\b|\bbulk " + _BAGS + r"|\bbig ?bags?\b|\bton(?:ne)? bags?\b"
                 r"|\bsuper ?sacks?\b|\bcontainer bags?\b|\bu ?panel\b|\bbaffle\b|\bq ?bags?\b"
                 r"|\b\d+(?:\.\d+)? ?(?:ton|tonne|mt) " + _BAGS),
    ("mono_carton", 9, r"\bmono ?cartons?\b|\bfolding (?:cartons?|box(?:es)?)\b|\bduplex\b|\bsbs\b|\bfbb\b"
                       r"|\bunit cartons?\b|\bpaperboard\b|\b(?:pharma|medicine|cosmetics?|perfume|soap|tea"
                       r"|sweet|mithai|chocolate|toy|rigid|printed) (?:box(?:es)?|cartons?)\b"),
    ("woven_sack", 9, r"\bad ?star\b|\badstar\b|\bcement " + _BAGS + r"|\bblock ?bottom\b|\bvalve (?:bags?|sacks?)\b"),
    ("film", 9, r"\bpouch(?:ing)? (?:films?|rolls?)\b|\b(?:packing|packaging) films?\b"),
    ("bopp_bag", 8, r"\bbopp\b(?! (?:films?|rolls?|tapes?|sheets?|labels?|wraps?)\b)"),
    ("film", 8, r"\b(?:shrink|stretch|cling|pallet|bopp|ldpe|lldpe|ld|pet|pe|polyester|metallised|metalized"
                r"|metallized|barrier|lamination|laminated|printed|plain|blown|cast|cpp|pvc|pof|mulch|sleeve)"
                r" (?:films?|wraps?|rolls?)\b|\bshrink\b|\bstretch\b|\bcling\b|\broll ?stock\b|\bpallet wrap"),
    ("pouch", 7, r"\bpouch(?:es)?\b|\bdoy ?packs?\b|\bzip ?lock\b|\bziplock\b|\bzipper\b|\bsachets?\b"
                 r"|\bstand ?up\b|\bspout(?:ed)?\b|\bretort\b|\b(?:3|three) side seal|\bcent(?:er|re) seal"
                 r"|\bpackets?\b|\bpoly ?bags?\b|\bpolythene (?:bags?|covers?)\b|\bldpe bags?\b"
                 r"|\bplastic (?:bags?|covers?)\b|\bthaili\b"),
    ("corrugated_box", 7, r"\bcorrugated\b|\b\d+ ?ply\b|\bmaster cartons?\b|\bshipping (?:box(?:es)?|cartons?)\b"
                          r"|\bcardboard\b|\bkraft (?:box(?:es)?|cartons?)\b|\bpeti\b|\bgatta\b|\brsc\b"
                          r"|\bpizza box|\bcarton box(?:es)?\b"),
    ("woven_sack", 6, r"\bwoven\b|\bsacks?\b|\b(?:pp|hdpe|polypropylene|raffia|laminated|plastic) "
                      r"(?:bags?|bori|katta|sacks?)\b|\bbori\b|\bboriya[n]?\b|\bkatt[ae]s?\b|\bleno\b|\bgunny\b"
                      r"|\b(?:rice|fertili[sz]er|feed|sugar|flour|atta|grain|wheat|seeds?|salt|urea|potato"
                      r"|onion|chemical|sand|maida|dal|pulses) (?:bags?|sacks?)\b"),
    ("film", 5, r"\bfilms?\b|\bwraps?\b|\bpanni\b|\brolls?\b"),
    ("corrugated_box", 4, r"\bcartons?\b|\bbox(?:es)?\b|\bdabb[ae]s?\b|\bdibb[ae]\b"),
    ("woven_sack", 1, r"\bbags?\b"),
]
_CATEGORY_RULES = [(c, w, re.compile(p)) for c, w, p in CATEGORY_RULES]


def _category(toks: list, nums: list, gsm: Optional[int], micron: Optional[int]) -> Optional[str]:
    text = " ".join(t.text for t in toks if t.kind != "punct" or t.text in "/")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    m_for = re.search(r"\b(?:for|ke liye)\b", text)
    hits = []
    for cat, weight, rx in _CATEGORY_RULES:
        for m in rx.finditer(text):
            hits.append([cat, weight, m.start()])
    if m_for:                                    # "film for pouches": the thing after "for" is the end use
        if any(h[2] < m_for.start() for h in hits):
            for h in hits:
                if h[2] > m_for.start():
                    h[1] /= 2
    if hits:
        cat, weight, _ = max(hits, key=lambda h: (h[1], -h[2]))
        has_ply = re.search(r"\bply\b|\bcorrugated\b|\bflute", text)
        if cat == "corrugated_box" and weight <= 4 and gsm and gsm >= 200 and not has_ply:
            return "mono_carton"                 # "printed carton 300 gsm" is board, not corrugated
        if cat == "woven_sack" and weight <= 1:   # generic "bags": a 250 kg+ bag is a bulk bag
            if any(n.kg and n.kg >= 250 for _, _, n in nums if n.role in ("kg", "mt")):
                return "fibc"
        return cat
    if micron:
        return "film"
    if re.search(r"\bply\b", text):
        return "corrugated_box"
    if gsm:
        return "mono_carton" if gsm >= 200 else "woven_sack"
    return None


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------
_STOP_PUNCT = frozenset(", ; | ( ) . \n - :".split(" ")) | {"\n"}


def _window_after(items: list, end: int, size: int = 4) -> list:
    out = []
    k = end + 1                                   # skip the unit token itself
    while k < len(items) and len(out) < size:
        it = items[k]
        if isinstance(it, Num) or it.text in _STOP_PUNCT:
            break
        out.append(it.text)
        k += 1
    return out


def _window_before(items: list, i: int, size: int = 3) -> list:
    out = []
    k = i - 1
    while k >= 0 and len(out) < size:
        it = items[k]
        if isinstance(it, Num) or it.text in _STOP_PUNCT:
            break
        out.insert(0, it.text)
        k -= 1
    return out


def _is_fill(items: list, i: int, end: int, n: Num, category: Optional[str]) -> bool:
    """Is this weight the size of one bag/pouch/box ("25 kg bag", "1000 kg capacity") rather than the order?"""
    v = n.kg or 0.0
    cap = FILL_MAX_KG.get(category, 100)
    after = _window_after(items, end)
    before = _window_before(items, i)
    if any(w in FILL_CUES_STRONG for w in after[:2]) or any(w in FILL_CUES_STRONG for w in before[-2:]):
        return True
    if before and before[-1] in ("of", "for") and len(before) >= 2 and before[-2] in CONTAINER_WORDS:
        return True                               # "bags of 50 kg", "bags for 25 kg"
    if category == "film":                        # film has no fill weight - only roll weight
        return v <= cap and any(w in ("roll", "rolls", "per") for w in after[:2])
    if v <= cap and (any(w in FILL_CUES_WEAK for w in after[:2] + before[-1:])
                     or any(w in CONTAINER_WORDS for w in after)):
        return True
    if n.cue:
        return False
    return v <= cap                               # "bopp bags 50 kg, 10000 pcs"


def _clean(v: float) -> Union[int, float]:
    v = round(v, 6)
    return int(v) if float(v).is_integer() else v


def _pick_quantity(items: list, nums: list, category: Optional[str]):
    for i, end, n in nums:
        if n.role in ("kg", "mt") and _is_fill(items, i, end, n, category):
            n.role = "fill"
    cands = [n for _, _, n in nums if n.role in ("count", "bare", "kg", "mt") and n.value > 0]
    if not cands:
        return None, None
    pool = [n for n in cands if not n.sample] or cands
    cued = [n for n in pool if n.cue]
    counts = [n for n in pool if n.role == "count"]
    weights = [n for n in pool if n.role in ("kg", "mt")]
    bares = [n for n in pool if n.role == "bare"]
    if cued:
        best = cued[0]
    elif counts:
        best = counts[0]
    elif weights:
        best = weights[0]
    else:
        best = max(bares, key=lambda n: (n.has_mult, n.value))
    unit = {"count": "pcs", "bare": "pcs", "kg": "kg", "mt": "mt"}[best.role]
    if best.role == "bare" and category == "film":
        unit = "kg"                               # film is traded by weight
    return _clean(best.value), unit


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
LOC_PRE_STRONG = _w("to deliver delivery delivered location loc city destination dest site address "
                    "ship shipping dispatch despatch send at port place area")
LOC_PRE_WEAK = _w("in near nr for")
LOC_POST = _w("delivery deliver mein me mai tak ko pe par site plant factory unit warehouse godown "
              "depot bhejna bhejo bhej pahuchana based chahiye")
LOC_FROM = _w("from ex")


def _display(toks: list) -> str:
    words = [t.orig for t in toks]
    joined = " ".join(words)
    key = _fold(joined)
    if key in UPPER_NAMES:
        return key.upper()
    if all(w.islower() or w.isupper() for w in words):
        return " ".join(w[:1].upper() + w[1:].lower() for w in words)
    return joined


def _location(items: list) -> Optional[str]:
    gaz = _gazetteer()
    letters = "".join(t.orig for t in items if isinstance(t, Tok) and t.kind == "word")
    mixed = any(c.isupper() for c in letters) and any(c.islower() for c in letters)
    words_all = [t.orig for t in items if isinstance(t, Tok) and t.kind == "word" and t.orig.isalpha()]
    if words_all and sum(w[0].isupper() for w in words_all) / len(words_all) >= 0.7:
        mixed = False                              # "Need Bopp Bags For Our Company": capitals mean nothing
    word_idx = [k for k, it in enumerate(items) if isinstance(it, Tok) and it.kind == "word"]
    if not word_idx:
        return None
    first_word, last_word = word_idx[0], word_idx[-1]
    L = len(items)

    def prev_word(k: int) -> str:
        j = k - 1
        while j >= 0 and _txt(items, j) in (":", "-", "=", "@"):
            j -= 1
        return _txt(items, j)

    def context_score(s: int, e: int) -> float:
        p1, p2 = prev_word(s), _txt(items, s - 2) if s >= 2 else ""
        nxt = _txt(items, e)
        score = 0.0
        if p1 in LOC_PRE_STRONG or (p1 in ("to", "at", "in") and p2 in LOC_PRE_STRONG):
            score += 3
        elif p1 in LOC_PRE_WEAK:
            score += 1
        if nxt in LOC_POST or (nxt == "ke" and _txt(items, e + 1) == "liye"):
            score += 2
        if p1 in LOC_FROM or nxt == "se":
            score -= 3
        tail = [it for it in items[e:] if isinstance(it, Tok) and it.kind == "word" and it.text not in VOCAB
                and gaz.get(_fold(it.orig), ("", "city"))[1] == "city"]   # "Dhaka Bangladesh": skip country
        if not tail:
            score += 1                            # nothing but filler after it: "..., Nagpur"
        return score + 0.5 * e / L

    cands = []   # (score, start, end, display)
    covered: set = set()
    for s in word_idx:
        for size in (3, 2, 1):
            span = items[s:s + size]
            if len(span) < size or not all(isinstance(t, Tok) and t.kind == "word" for t in span):
                continue
            key = _fold(" ".join(t.orig for t in span))
            entry = gaz.get(key)
            if entry is None:
                continue
            disp, kind, pop, _, _ = entry
            cap = mixed and span[0].orig[:1].isupper() and s != first_word   # "Dar es Salaam"
            strong = prev_word(s) in LOC_PRE_STRONG
            post = _txt(items, s + 1) in LOC_POST
            alone = _txt(items, s - 1) in _STOP_PUNCT | {""} and _txt(items, s + 1) in _STOP_PUNCT | {""}
            if size == 1 and key in _SOFT_BLOCK:       # ", Mau," standing alone is a place
                if not (cap or strong or post or alone or s == last_word):
                    continue
            elif key in GAZ_BLOCK or all(t.text in VOCAB or _fold(t.orig) in VOCAB for t in span):
                if not (cap and strong):
                    continue
            score = 1.0 + context_score(s, s + size) + (1.0 if cap else 0.0)
            score += {"city": 0.0, "country": -1.5, "state": -1.5}[kind] + 0.1 * math.log10(pop + 1)
            if any(k in covered for k in range(s, s + size)):
                continue
            cands.append((score, s, s + size, _display(span)))
            covered.update(range(s, s + size))
            if kind in ("state", "country"):         # "Siddhpur Gujarat": unknown town before a state
                j = s - 2 if _txt(items, s - 1) == "," else s - 1
                town = items[j] if j >= 0 else None
                if (isinstance(town, Tok) and town.kind == "word" and town.orig.isalpha() and len(town.orig) >= 3
                        and town.text not in VOCAB and _fold(town.orig) not in gaz and j not in covered
                        and (town.orig[:1].isupper() or not mixed)):
                    cands.append((score + 2.5, j, j + 1, _display([town])))
            break
    if cands:
        return max(cands, key=lambda c: (c[0], c[1]))[3]

    # Not in the gazetteer: typo of a known city, or an unknown Capitalised name after a cue.
    best = None
    for s in word_idx:
        t = items[s]
        if t.text in VOCAB or not t.orig.isalpha() or len(t.orig) < 3:
            continue
        p1 = prev_word(s)
        strong = p1 in LOC_PRE_STRONG or (p1 in ("to", "at", "in") and _txt(items, s - 2) in LOC_PRE_STRONG)
        post = _txt(items, s + 1) in LOC_POST
        cap = mixed and t.orig[:1].isupper() and s != first_word
        is_last = all(not (isinstance(it, Tok) and it.kind == "word" and it.text not in VOCAB)
                      for it in items[s + 1:])
        if not (strong or post or cap or s == last_word):
            continue
        key = _fold(t.orig)
        fuzzy = None
        if len(key) >= 5 and key.isascii():
            limit = 1 if len(key) < 10 else 2
            options = [(d, not main, -pop, disp) for k2, disp, main, pop in _fuzzy_city_index().get(key[0], [])
                       if (d := _osa(key, k2, limit)) <= limit]
            if options:
                fuzzy = min(options)[3]               # closest; prefer the main name ("Ahmedabad")
        score = context_score(s, s + 1) + (1 if cap else 0)
        if fuzzy:
            cand = (score + 1, s, fuzzy)
        elif strong or (cap and (post or is_last)):
            cand = (score, s, _display([t]))
        else:
            continue
        if best is None or cand[:2] > best[:2]:
            best = cand
    return best[2] if best else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _empty() -> dict:
    return {"category": None, "quantity": None, "unit": None, "gsm": None, "micron": None, "location": None}


def _parse(text: str, out: dict):
    """Fill `out` field by field (so a late failure keeps the earlier fields). Returns (items, nums)."""
    toks = _tokenise(_normalise(text))
    for t in toks:
        if t.kind == "word":
            t.text = _fix_typo(t.text)
    items = _read_numbers(toks)
    nums = _label_numbers(items)
    gsm = next((n.value for _, _, n in nums if n.role == "gsm"), None)
    mic = next((n.micron for _, _, n in nums if n.role == "micron" and n.micron), None)
    out["gsm"] = int(round(gsm)) if gsm is not None else None
    out["micron"] = int(round(mic)) if mic is not None else None
    out["category"] = _category(toks, nums, out["gsm"], out["micron"])
    out["quantity"], out["unit"] = _pick_quantity(items, nums, out["category"])
    out["location"] = _location(items)
    return items, nums


def parse_rfq(text: str) -> dict:
    """Parse one buyer message into {category, quantity, unit, gsm, micron, location}."""
    out = _empty()
    if not isinstance(text, str) or not text.strip():
        return out
    try:
        _parse(text, out)
    except Exception:  # never crash the caller on one odd message
        if os.environ.get("RFQ_DEBUG"):
            raise
    return out


def explain(text: str) -> str:
    """Human-readable trace: every number with the role it was given, then the result."""
    out = _empty()
    items, _ = _parse(text, out)
    shown = []
    for it in items:
        if isinstance(it, Num):
            shown.append(f"[{_clean(it.value)} -> {it.role}{' (qty cue)' if it.cue else ''}]")
        elif it.kind != "punct":
            shown.append(it.text)
    return " ".join(shown) + "\n  => " + json.dumps(out, ensure_ascii=False)


if __name__ == "__main__":
    args = sys.argv[1:]
    verbose = bool(args) and args[0] == "--explain"
    messages = (args[1:] if verbose else args) or [line.rstrip("\n") for line in sys.stdin]
    for message in messages:
        if verbose:
            print(message, "\n ", explain(message))
        else:
            print(json.dumps({"text": message, "parsed": parse_rfq(message)}, ensure_ascii=False))
