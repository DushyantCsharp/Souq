"""Vocabulary for the rule parser. Surface forms are written naturally and normalised at load time with
``souq_common.arabic.normalise_arabic`` so they compare equal to normalised query tokens.

The static lists cover the common shopping vocabulary; :func:`install_catalog_values` extends brands,
colours and sizes with the values that actually occur in the index (loaded by the service at startup).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from souq_common.arabic import normalise_arabic


def norm(s: str) -> str:
    return normalise_arabic(s).lower().strip()


# --- Currency -----------------------------------------------------------------------------------------
_CURRENCY_RAW: dict[str, list[str]] = {
    "AED": [
        "aed",
        "dhs",
        "dh",
        "dhs.",
        "dirham",
        "dirhams",
        "درهم",
        "دراهم",
        "درهما",
        "د.إ",
        "دإ",
        "dirham",
        "drahem",
        "darahem",
        "dra7em",
        "derham",
    ],
    "SAR": ["sar", "riyal", "riyals", "ريال", "ريالات", "ر.س", "rial"],
    "USD": ["usd", "$", "dollar", "dollars", "دولار", "دولارات", "dolar", "bucks"],
    "EUR": ["eur", "€", "euro", "euros", "يورو"],
    "GBP": ["gbp", "£", "pound", "pounds", "جنيه استرليني"],
    "EGP": ["egp", "جنيه", "جنيهات", "geneh"],
    "KWD": ["kwd", "دينار كويتي"],
}
CURRENCIES: dict[str, str] = {norm(s): code for code, forms in _CURRENCY_RAW.items() for s in forms}

# --- Price operators (token sequences, normalised) ------------------------------------------------------
_MAX_OPS_RAW = [
    "under",
    "below",
    "less than",
    "lower than",
    "cheaper than",
    "up to",
    "upto",
    "max",
    "maximum",
    "at most",
    "no more than",
    "not more than",
    "within",
    "<",
    "<=",
    "≤",
    "budget",
    "budget of",
    "under than",
    "أقل من",
    "باقل من",
    "بأقل من",
    "اقل",
    "ارخص من",
    "أرخص من",
    "تحت",
    "دون",
    "حتى",
    "لحد",
    "لغايه",
    "بحد أقصى",
    "حد أقصى",
    "اقصى",
    "لا يزيد عن",
    "ما يزيد عن",
    "ميزانيه",
    "بميزانيه",
    "في حدود",
    "ta7t",
    "taht",
    "tahet",
    "a2al men",
    "a2al min",
    "a2al mn",
    "aqal min",
    "aqal men",
    "akal men",
    "a2al",
    "ar5as men",
    "arkhas men",
    "la7ad",
    "lahad",
]
_MIN_OPS_RAW = [
    "over",
    "above",
    "more than",
    "greater than",
    "higher than",
    "at least",
    "min",
    "minimum",
    "starting from",
    "starting at",
    "from",
    ">",
    ">=",
    "≥",
    "upwards of",
    "أكثر من",
    "اكثر",
    "أعلى من",
    "فوق",
    "لا يقل عن",
    "ابتداء من",
    "ابتداءا من",
    "يبدأ من",
    "من",
    "akthar men",
    "aktar men",
    "aktar min",
    "akthar min",
    "fo2",
    "fou2",
    "fawq",
    "fo2 el",
]
# Number-then-operator tails: "300 aed or less", "200+"
_MAX_TAILS_RAW = [
    "or less",
    "or under",
    "or below",
    "and under",
    "and below",
    "max",
    "or cheaper",
    "او اقل",
    "أو أقل",
    "وأقل",
    "و اقل",
    "aw a2al",
    "wa a2al",
]
_MIN_TAILS_RAW = [
    "or more",
    "and above",
    "and up",
    "and over",
    "plus",
    "+",
    "او اكثر",
    "أو أكثر",
    "وفوق",
    "و فوق",
    "واكثر",
    "aw aktar",
    "w fo2",
]
_RANGE_START_RAW = [
    "between",
    "from",
    "بين",
    "من",
    "ما بين",
    "bein",
    "bain",
    "ben",
    "men",
    "min",
    "price",
    "priced",
    "price range",
    "سعر",
    "بسعر",
    "السعر",
]
_RANGE_SEP_RAW = [
    "and",
    "to",
    "-",
    "–",
    "و",
    "الى",
    "إلى",
    "حتى",
    "لـ",
    "ل",
    "w",
    "wa",
    "ila",
    "la",
    "lel",
    "till",
    "until",
    "through",
]
# Tokens that may precede a price expression and belong to it ("shoes for under 300", "بسعر اقل من").
_PRICE_LEADS_RAW = [
    "for",
    "at",
    "with",
    "price",
    "priced",
    "costing",
    "cost",
    "سعر",
    "بسعر",
    "السعر",
    "سعره",
    "ب",
    "with price",
    "b",
    "bi",
]


def _seqs(items: list[str]) -> list[tuple[str, ...]]:
    out = {tuple(norm(x).split()) for x in items if norm(x)}
    return sorted(out, key=len, reverse=True)  # longest match first


MAX_OPS = _seqs(_MAX_OPS_RAW)
MIN_OPS = _seqs(_MIN_OPS_RAW)
MAX_TAILS = _seqs(_MAX_TAILS_RAW)
MIN_TAILS = _seqs(_MIN_TAILS_RAW)
RANGE_START = _seqs(_RANGE_START_RAW)
RANGE_SEP = {norm(x) for x in _RANGE_SEP_RAW}
PRICE_LEADS = {norm(x) for x in _PRICE_LEADS_RAW}
# Operators that are also ordinary words: only treated as prices when a currency is present too.
WEAK_OPS = {
    ("from",),
    ("من",),
    ("min",),
    ("men",),
    ("حتى",),
    ("دون",),
    ("max",),
    ("اقل",),
    ("اكثر",),
    ("a2al",),
    ("budget",),
    ("within",),
    ("فوق",),
    ("تحت",),
}

# --- Sizes ----------------------------------------------------------------------------------------------
_SIZE_CUES_RAW = [
    "size",
    "sz",
    "sizes",
    "size:",
    "مقاس",
    "قياس",
    "مقاسات",
    "مقاسه",
    "بمقاس",
    "المقاس",
    "ma2as",
    "maqas",
    "m2as",
    "qyas",
    "حجم",
]
SIZE_CUES = {norm(x) for x in _SIZE_CUES_RAW}
SHOE_SYSTEM_CUES = {"eu", "uk", "us"}
_LETTER_SIZES_RAW: dict[str, str] = {
    "xxs": "XXS",
    "xs": "XS",
    "s": "S",
    "m": "M",
    "l": "L",
    "xl": "XL",
    "xxl": "XXL",
    "xxxl": "XXXL",
    "2xl": "XXL",
    "3xl": "XXXL",
    "4xl": "4XL",
    "5xl": "5XL",
    "small": "S",
    "medium": "M",
    "large": "L",
    "x-large": "XL",
    "xlarge": "XL",
    "extra large": "XL",
    "xx-large": "XXL",
    "x-small": "XS",
    "صغير": "S",
    "سمول": "S",
    "وسط": "M",
    "متوسط": "M",
    "ميديم": "M",
    "كبير": "L",
    "لارج": "L",
    "اكس لارج": "XL",
    "كبير جدا": "XL",
    "sghir": "S",
    "zghir": "S",
    "wasat": "M",
    "kbir": "L",
    "kabir": "L",
    "free size": "FREE",
    "one size": "ONE SIZE",
    "فري سايز": "FREE",
}
LETTER_SIZES: dict[tuple[str, ...], str] = {tuple(norm(k).split()): v for k, v in _LETTER_SIZES_RAW.items()}
# Unambiguous on their own (no "size" cue needed). Single letters and "large" are not.
STANDALONE_SIZES = {("xxs",), ("xs",), ("xl",), ("xxl",), ("xxxl",), ("2xl",), ("3xl",), ("4xl",), ("5xl",)}
SIZE_NUMBER = re.compile(r"^\d{1,3}(?:\.5)?$")

# --- Colours: surface -> canonical candidates (first one present in the catalog wins) --------------------
_COLOURS_RAW: dict[str, list[str]] = {
    "black": ["black", "blk", "اسود", "أسود", "سوداء", "سودا", "aswad", "sawda", "iswid"],
    "white": ["white", "wht", "ابيض", "أبيض", "بيضاء", "بيضا", "abyad", "bayda", "abiad"],
    "red": ["red", "احمر", "أحمر", "حمراء", "حمرا", "ahmar", "a7mar", "hamra", "7amra", "7amra2"],
    "blue": ["blue", "ازرق", "أزرق", "زرقاء", "زرقا", "azraq", "azra2", "zarqa", "zar2a", "azrak"],
    "green": ["green", "اخضر", "أخضر", "خضراء", "خضرا", "akhdar", "a5dar", "khadra", "5adra"],
    "yellow": ["yellow", "اصفر", "أصفر", "صفراء", "صفرا", "asfar", "safra"],
    "purple": ["purple", "violet", "lilac", "lavender", "بنفسجي", "موف", "ليلكي", "banafsaji", "mov"],
    "pink": ["pink", "وردي", "زهري", "بمبي", "روز", "wardi", "zahri", "bambi"],
    "brown": ["brown", "بني", "bonni", "bunni", "bni"],
    "grey": ["grey", "gray", "رمادي", "رصاصي", "سكني", "rmadi", "ramadi", "rasasi"],
    "beige": ["beige", "بيج", "bej", "beij"],
    "navy": ["navy", "navy blue", "كحلي", "kohli", "ka7li", "k7li"],
    "orange": ["orange", "برتقالي", "برتقاني", "bortoqali", "bordo2ani", "burtu2ali"],
    "gold": ["gold", "golden", "ذهبي", "dahabi", "thahabi"],
    "silver": ["silver", "فضي", "fiddi", "fedi", "fiddy"],
    "maroon": ["maroon", "burgundy", "wine", "عنابي", "خمري", "نبيتي", "3annabi"],
    "turquoise": ["turquoise", "teal", "تركواز", "فيروزي", "turkuaz"],
    "cream": ["cream", "ivory", "off-white", "off white", "كريمي", "سكري", "اوف وايت"],
    "clear": ["clear", "transparent", "شفاف", "شفافه", "shafaf"],
    "multicolour": [
        "multicolour",
        "multicolor",
        "multi-colour",
        "multi-color",
        "colourful",
        "colorful",
        "ملون",
        "متعدد الالوان",
        "mlawan",
    ],
}
COLOUR_FALLBACK = {"navy": "blue", "maroon": "red", "turquoise": "blue", "cream": "white", "gold": "yellow"}
COLOURS: dict[tuple[str, ...], str] = {
    tuple(norm(s).split()): canonical for canonical, forms in _COLOURS_RAW.items() for s in forms
}
# English words that are colours only in context ("orange shirt" yes, "orange juice" no).
AMBIGUOUS_COLOURS = {
    ("clear",),
    ("orange",),
    ("gold",),
    ("silver",),
    ("wine",),
    ("cream",),
    ("ivory",),
    ("rose",),
    ("navy",),
    ("teal",),
    ("lilac",),
    ("lavender",),
    ("golden",),
    ("roz",),
    ("روز",),
}
COLOUR_CUES = {
    norm(x)
    for x in [
        "color",
        "colour",
        "colored",
        "coloured",
        "in",
        "لون",
        "اللون",
        "باللون",
        "بلون",
        "لونه",
        "loon",
        "lon",
        "color:",
        "colour:",
    ]
}

# Product nouns that make an ambiguous colour word a colour ("gold watch", "silver ring").
PRODUCT_NOUNS = {
    norm(x)
    for x in [
        "shirt",
        "shirts",
        "tshirt",
        "t-shirt",
        "dress",
        "dresses",
        "bag",
        "bags",
        "handbag",
        "shoes",
        "shoe",
        "sneakers",
        "trainers",
        "boots",
        "sandals",
        "heels",
        "jacket",
        "coat",
        "hoodie",
        "sweater",
        "jeans",
        "pants",
        "trousers",
        "shorts",
        "skirt",
        "abaya",
        "scarf",
        "hijab",
        "watch",
        "watches",
        "ring",
        "necklace",
        "bracelet",
        "earrings",
        "phone",
        "case",
        "cover",
        "laptop",
        "headphones",
        "earbuds",
        "chair",
        "sofa",
        "table",
        "lamp",
        "curtains",
        "rug",
        "pillow",
        "towel",
        "backpack",
        "wallet",
        "belt",
        "cap",
        "hat",
        "socks",
        "suit",
        "blouse",
        "top",
        "kurta",
        "kandura",
        "thobe",
        "dishdasha",
        "قميص",
        "فستان",
        "شنطه",
        "حقيبه",
        "حذاء",
        "جزمه",
        "ساعه",
        "خاتم",
        "سلسال",
        "عقد",
        "اسواره",
        "جوال",
        "كفر",
        "سماعه",
        "سماعات",
        "كرسي",
        "كنبه",
        "طاوله",
        "عبايه",
        "شيله",
        "طرحه",
        "جاكيت",
        "بنطلون",
    ]
}

# --- Brands (static seed; catalog brands are added at startup) -------------------------------------------
_BRANDS_RAW: dict[str, list[str]] = {
    "Nike": ["nike", "نايك", "نايكي", "naik", "naiki"],
    "Adidas": ["adidas", "addidas", "adiddas", "اديداس", "أديداس", "adedas"],
    "Puma": ["puma", "بوما"],
    "Reebok": ["reebok", "ريبوك"],
    "New Balance": ["new balance", "newbalance", "نيو بالانس"],
    "Under Armour": ["under armour", "under armor", "اندر ارمور"],
    "Skechers": ["skechers", "sketchers", "سكيتشرز", "سكتشرز"],
    "ASICS": ["asics", "اسيكس"],
    "Converse": ["converse", "كونفرس"],
    "Vans": ["vans", "فانز"],
    "Apple": ["apple", "ابل", "آبل", "ايبل"],
    "Samsung": ["samsung", "سامسونج", "سامسونغ", "سامسونق", "samsong"],
    "Sony": ["sony", "سوني"],
    "LG": ["lg", "ال جي", "إل جي"],
    "Huawei": ["huawei", "هواوي"],
    "Xiaomi": ["xiaomi", "شاومي", "شياومي", "redmi", "ريدمي"],
    "OPPO": ["oppo", "اوبو"],
    "Nokia": ["nokia", "نوكيا"],
    "Lenovo": ["lenovo", "لينوفو"],
    "HP": ["hp", "اتش بي"],
    "Dell": ["dell", "ديل"],
    "ASUS": ["asus", "اسوس"],
    "Acer": ["acer", "ايسر"],
    "Microsoft": ["microsoft", "مايكروسوفت"],
    "Logitech": ["logitech", "لوجيتك"],
    "JBL": ["jbl", "جي بي ال"],
    "Bose": ["bose", "بوز"],
    "Beats": ["beats", "بيتس"],
    "Anker": ["anker", "انكر"],
    "Canon": ["canon", "كانون"],
    "Nikon": ["nikon", "نيكون"],
    "Philips": ["philips", "فيليبس"],
    "Braun": ["braun", "براون"],
    "Dyson": ["dyson", "دايسون"],
    "Tefal": ["tefal", "تيفال"],
    "Kenwood": ["kenwood", "كينوود"],
    "Bosch": ["bosch", "بوش"],
    "Casio": ["casio", "كاسيو"],
    "Rolex": ["rolex", "رولكس"],
    "Fossil": ["fossil", "فوسيل"],
    "Seiko": ["seiko", "سيكو"],
    "Zara": ["zara", "زارا"],
    "H&M": ["h&m", "h & m", "اتش اند ام"],
    "Gucci": ["gucci", "غوتشي", "قوتشي", "جوتشي"],
    "Lacoste": ["lacoste", "لاكوست"],
    "Chanel": ["chanel", "شانيل", "شانل"],
    "Dior": ["dior", "ديور"],
    "Levi's": ["levi's", "levis", "ليفايز"],
    "Calvin Klein": ["calvin klein", "كالفن كلاين"],
    "Tommy Hilfiger": ["tommy hilfiger", "تومي هيلفيغر"],
    "Michael Kors": ["michael kors", "مايكل كورس"],
    "Ray-Ban": ["ray-ban", "rayban", "ray ban", "راي بان", "ريبان"],
    "LEGO": ["lego", "ليغو", "ليجو"],
    "Nivea": ["nivea", "نيفيا"],
    "L'Oreal": ["l'oreal", "loreal", "لوريال"],
    "Pampers": ["pampers", "بامبرز"],
    "IKEA": ["ikea", "ايكيا"],
    "Amazon Basics": ["amazon basics", "amazonbasics"],
    "Hamilton Beach": ["hamilton beach"],
    "Crocs": ["crocs", "كروكس"],
    "The North Face": ["the north face", "north face", "نورث فيس"],
    "Tissot": ["tissot", "تيسوت"],
    "Garmin": ["garmin", "جارمن"],
    "Fitbit": ["fitbit", "فيتبيت"],
    "Xbox": ["xbox", "اكس بوكس"],
    "PlayStation": ["playstation", "بلايستيشن", "بلاي ستيشن"],
    "Nintendo": ["nintendo", "نينتندو"],
}

# Catalog brands that are also everyday words must not become hard filters.
COMMON_WORDS = {
    norm(x)
    for x in [
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "with",
        "without",
        "of",
        "in",
        "on",
        "to",
        "by",
        "new",
        "set",
        "pack",
        "kit",
        "case",
        "cover",
        "home",
        "kitchen",
        "wireless",
        "bluetooth",
        "portable",
        "smart",
        "mini",
        "pro",
        "max",
        "plus",
        "ultra",
        "classic",
        "original",
        "premium",
        "generic",
        "unknown",
        "unbranded",
        "none",
        "na",
        "n/a",
        "other",
        "various",
        "assorted",
        "basic",
        "basics",
        "essentials",
        "best",
        "top",
        "good",
        "great",
        "value",
        "black",
        "white",
        "red",
        "blue",
        "green",
        "women",
        "men",
        "kids",
        "baby",
        "girls",
        "boys",
        "large",
        "small",
        "medium",
        "big",
        "little",
        "light",
        "dark",
        "gold",
        "silver",
        "glass",
        "wood",
        "metal",
        "steel",
        "plastic",
        "leather",
        "cotton",
        "silk",
        "water",
        "fire",
        "sun",
        "moon",
        "star",
        "king",
        "queen",
        "happy",
        "love",
        "life",
        "style",
        "fashion",
        "sport",
        "sports",
        "outdoor",
        "indoor",
        "office",
        "travel",
        "car",
        "auto",
        "power",
        "energy",
        "tech",
        "digital",
        "electric",
        "electronics",
        "usb",
        "led",
        "hd",
        "4k",
        "one",
        "two",
        "three",
        "first",
        "only",
        "just",
        "more",
        "less",
        "under",
        "over",
        "shoes",
        "shoe",
        "bag",
        "bags",
        "watch",
        "phone",
        "tv",
        "cable",
        "charger",
        "light",
        "lamp",
        "book",
        "books",
        "toy",
        "toys",
        "game",
        "games",
    ]
}


@dataclass
class Lexicon:
    brands: dict[tuple[str, ...], str]
    colours: dict[tuple[str, ...], str]
    catalog_colours: set[str] = field(default_factory=set)
    catalog_sizes: set[str] = field(default_factory=set)
    max_brand_len: int = 3

    def resolve_colour(self, canonical: str) -> str:
        if self.catalog_colours and canonical not in self.catalog_colours:
            fallback = COLOUR_FALLBACK.get(canonical)
            if fallback and fallback in self.catalog_colours:
                return fallback
        return canonical


def _static_brands() -> dict[tuple[str, ...], str]:
    return {tuple(norm(s).split()): name for name, forms in _BRANDS_RAW.items() for s in forms}


_lexicon = Lexicon(brands=_static_brands(), colours=dict(COLOURS))


def get_lexicon() -> Lexicon:
    return _lexicon


def install_catalog_values(
    brands: dict[str, int] | None = None,
    colours: set[str] | None = None,
    sizes: set[str] | None = None,
    min_brand_docs: int = 3,
) -> Lexicon:
    """Extend the static lexicon with catalog values (brand -> doc count, canonical colours, sizes)."""
    global _lexicon
    merged = _static_brands()
    for brand, count in (brands or {}).items():
        key = tuple(norm(brand).split())
        if not key or count < min_brand_docs or len(key) > 4:
            continue
        if len(key) == 1 and (len(key[0]) < 3 or key[0] in COMMON_WORDS or key[0].isdigit()):
            continue
        if key in COLOURS or key in LETTER_SIZES:
            continue
        merged.setdefault(key, brand)
    _lexicon = Lexicon(
        brands=merged,
        colours=dict(COLOURS),
        catalog_colours={c.lower() for c in (colours or set())},
        catalog_sizes={s.upper() for s in (sizes or set())},
        max_brand_len=max((len(k) for k in merged), default=3),
    )
    return _lexicon


def reset_lexicon() -> None:
    global _lexicon
    _lexicon = Lexicon(brands=_static_brands(), colours=dict(COLOURS))


# --- Constraint-like leftovers: send the query to the LLM parser ------------------------------------------
# Only words the LLM can turn into a Filters value. Negation ("without", "بدون") is deliberately absent:
# Filters cannot express exclusions, and "بدون أكمام" (sleeveless) is a product attribute anyway.
PRICE_WORDS = {
    norm(x)
    for x in [
        "cheap",
        "cheaper",
        "cheapest",
        "affordable",
        "inexpensive",
        "budget",
        "expensive",
        "pricey",
        "luxury",
        "luxurious",
        "discount",
        "discounted",
        "deal",
        "deals",
        "sale",
        "clearance",
        "bargain",
        "lowest",
        "highest",
        "bucks",
        "رخيص",
        "رخيصه",
        "ارخص",
        "اقتصادي",
        "اقتصاديه",
        "غالي",
        "غاليه",
        "عرض",
        "عروض",
        "تخفيض",
        "تخفيضات",
        "خصم",
        "خصومات",
        "ra5ees",
        "rkhis",
        "rakhees",
        "rkhees",
        "ar5as",
        "arkhas",
        "ghali",
        "3ard",
        "takhfid",
    ]
}
APPROX_WORDS = {
    norm(x)
    for x in [
        "around",
        "approx",
        "approximately",
        "roughly",
        "~",
        "حوالي",
        "تقريبا",
        "بحدود",
        "7awali",
        "ta2riban",
    ]
}
OPERATOR_WORDS = {
    norm(x)
    for x in [
        "under",
        "below",
        "less",
        "over",
        "above",
        "more",
        "max",
        "min",
        "between",
        "upto",
        "about",
        "اقل",
        "اكثر",
        "تحت",
        "فوق",
        "بين",
        "ta7t",
        "fo2",
    ]
}
MAGNITUDE_WORDS = {
    norm(x)
    for x in [
        "hundred",
        "hundreds",
        "thousand",
        "thousands",
        "grand",
        "k",
        "مية",
        "مئة",
        "مائة",
        "ميه",
        "مايه",
        "ميتين",
        "مئتين",
        "مئتان",
        "مئتي",
        "الف",
        "الفين",
        "الاف",
        "ثلاثمية",
        "ثلاثمائة",
        "اربعمية",
        "خمسمية",
        "خمسمائة",
        "ستمية",
        "سبعمية",
        "ثمانمية",
        "تسعمية",
        "miya",
        "mit",
        "alf",
    ]
}
EN_NUMBER_WORDS = {
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "fifteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
    "a",
    "عشرين",
    "ثلاثين",
    "اربعين",
    "خمسين",
    "ستين",
    "سبعين",
    "ثمانين",
    "تسعين",
}
