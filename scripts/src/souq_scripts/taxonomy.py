"""Seed catalog taxonomy: categories with bilingual nouns, AED price bands, brands and keyword rules.

ESCI ships no categories, so seeded products are classified from their titles with these keyword rules.
The same table drives the synthetic supplier feed and per-category price plausibility, which keeps the
three seeds consistent with each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

from souq_scripts.category import slugify_category

SizeKind = Literal["apparel", "shoe", "none"]


@dataclass(frozen=True)
class Category:
    path: str
    noun_en: str
    noun_ar: str
    price_aed: tuple[float, float]
    keywords: tuple[str, ...]
    brands: tuple[str, ...] = ()
    size_kind: SizeKind = "none"
    # Accessories outrank the device they accessorise: "case for iPhone 15" is a case, not a phone.
    priority: int = 0
    materials: tuple[str, ...] = field(default=())

    @property
    def ltree(self) -> str:
        lt = slugify_category(self.path)
        assert lt is not None, self.path
        return lt


_E = ("Sony", "Samsung", "Apple", "LG", "Xiaomi", "Huawei", "Lenovo", "HP", "Dell", "Asus", "Anker", "JBL")
_F = (
    "Nike",
    "Adidas",
    "Puma",
    "Levi's",
    "Zara",
    "H&M",
    "Mango",
    "Tommy Hilfiger",
    "Calvin Klein",
    "Skechers",
)
_H = ("IKEA", "Tefal", "Philips", "Braun", "Kenwood", "Black+Decker", "Pyrex", "Home Centre", "Bosch")
_B = ("L'Oreal", "Nivea", "Maybelline", "Garnier", "The Ordinary", "Dove", "Gillette", "Ajmal", "Arabian Oud")

TAXONOMY: tuple[Category, ...] = (
    Category(
        "Electronics > Audio > Headphones",
        "Headphones",
        "سماعات رأس",
        (39, 1899),
        ("headphone", "headphones", "earbuds", "earbud", "earphones", "earphone", "airpods", "headset"),
        ("Sony", "JBL", "Apple", "Bose", "Anker", "Sennheiser", "Beats"),
        priority=2,
    ),
    Category(
        "Electronics > Audio > Speakers",
        "Speaker",
        "مكبر صوت",
        (49, 2499),
        ("speaker", "speakers", "soundbar", "subwoofer", "sound bar"),
        ("JBL", "Sony", "Bose", "Anker"),
    ),
    Category(
        "Electronics > Mobile Phones > Smartphones",
        "Smartphone",
        "هاتف ذكي",
        (399, 6999),
        ("smartphone", "iphone", "galaxy", "android phone", "unlocked phone", "cell phone", "pixel"),
        ("Apple", "Samsung", "Xiaomi", "Huawei", "Oppo", "Google"),
    ),
    Category(
        "Electronics > Mobile Phones > Cases & Covers",
        "Phone Case",
        "جراب هاتف",
        (15, 199),
        (
            "phone case",
            "iphone case",
            "galaxy case",
            "case for",
            "case compatible",
            "protective case",
            "wallet case",
            "phone cover",
            "screen protector",
            "tempered glass",
            "folio case",
        ),
        ("Spigen", "OtterBox", "ESR", "Anker"),
        priority=4,
    ),
    Category(
        "Electronics > Mobile Phones > Chargers & Cables",
        "Charger",
        "شاحن",
        (19, 299),
        (
            "charger",
            "charging",
            "charging cable",
            "usb cable",
            "lightning cable",
            "power bank",
            "wall adapter",
            "usb c",
            "usb-c",
            "magsafe",
        ),
        ("Anker", "Belkin", "Ugreen", "Baseus", "Apple", "Samsung"),
        priority=3,
    ),
    Category(
        "Electronics > Computers > Laptops",
        "Laptop",
        "حاسوب محمول",
        (999, 12999),
        ("laptop", "chromebook", "macbook", "notebook computer", "ultrabook"),
        ("Lenovo", "HP", "Dell", "Asus", "Apple", "Acer", "MSI"),
        priority=1,
    ),
    Category(
        "Electronics > Computers > Monitors",
        "Monitor",
        "شاشة كمبيوتر",
        (399, 3999),
        ("monitor", "display"),
        ("LG", "Samsung", "Dell", "Asus", "BenQ"),
    ),
    Category(
        "Electronics > Computers > Accessories",
        "Computer Accessory",
        "ملحقات الكمبيوتر",
        (25, 899),
        ("keyboard", "mouse", "webcam", "usb hub", "docking station", "mouse pad", "laptop stand"),
        ("Logitech", "Razer", "Microsoft", "Anker", "HP"),
        priority=3,
    ),
    Category(
        "Electronics > Computers > Storage",
        "Storage Drive",
        "وحدة تخزين",
        (29, 1299),
        ("ssd", "hard drive", "flash drive", "sd card", "memory card", "micro sd", "usb drive"),
        ("SanDisk", "Samsung", "Kingston", "Western Digital", "Seagate"),
        priority=2,
    ),
    Category(
        "Electronics > TV & Video > Televisions",
        "Television",
        "تلفزيون",
        (899, 14999),
        ("tv", "television", "smart tv", "oled", "qled"),
        ("Samsung", "LG", "Sony", "TCL", "Hisense"),
    ),
    Category(
        "Electronics > Cameras > Cameras & Lenses",
        "Camera",
        "كاميرا",
        (99, 8999),
        ("camera", "lens", "dslr", "mirrorless", "gopro", "tripod", "camcorder"),
        ("Canon", "Nikon", "Sony", "Fujifilm", "GoPro"),
    ),
    Category(
        "Electronics > Wearables > Smartwatches",
        "Smartwatch",
        "ساعة ذكية",
        (149, 3499),
        ("smartwatch", "smart watch", "fitness tracker", "apple watch", "galaxy watch", "watch band"),
        ("Apple", "Samsung", "Huawei", "Garmin", "Amazfit", "Fitbit"),
        priority=2,
    ),
    Category(
        "Electronics > Gaming > Consoles & Accessories",
        "Gaming Accessory",
        "ألعاب فيديو",
        (49, 2999),
        ("playstation", "ps5", "ps4", "xbox", "nintendo", "switch", "controller", "gaming"),
        ("Sony", "Microsoft", "Nintendo", "Razer", "Logitech"),
    ),
    Category(
        "Fashion > Women > Dresses",
        "Dress",
        "فستان",
        (59, 899),
        ("dress", "dresses", "abaya", "gown", "maxi"),
        ("Zara", "Mango", "H&M", "Shein", "Max"),
        "apparel",
        materials=("cotton", "polyester", "chiffon", "linen"),
    ),
    Category(
        "Fashion > Clothing > Shirts & Tops",
        "Shirt",
        "قميص",
        (29, 399),
        ("shirt", "t-shirt", "tshirt", "tee", "polo", "blouse", "tank top", "top"),
        _F,
        "apparel",
        materials=("cotton", "polyester", "linen"),
    ),
    Category(
        "Fashion > Clothing > Trousers & Jeans",
        "Trousers",
        "بنطلون",
        (49, 499),
        ("jeans", "pants", "trousers", "shorts", "leggings", "joggers", "chinos"),
        _F,
        "apparel",
        materials=("denim", "cotton", "polyester"),
    ),
    Category(
        "Fashion > Clothing > Jackets & Knitwear",
        "Jacket",
        "جاكيت",
        (79, 1299),
        ("jacket", "coat", "trenchcoat", "hoodie", "sweatshirt", "sweater", "cardigan", "fleece", "vest"),
        _F,
        "apparel",
        materials=("cotton", "polyester", "wool", "fleece"),
    ),
    Category(
        "Fashion > Clothing > Underwear & Socks",
        "Socks",
        "جوارب",
        (19, 199),
        (
            "socks",
            "underwear",
            "boxer",
            "briefs",
            "bra",
            "panties",
            "undershirt",
            "tights",
            "stockings",
            "compression",
        ),
        ("Calvin Klein", "Hanes", "Tommy Hilfiger", "Nike"),
        "apparel",
        materials=("cotton",),
    ),
    Category(
        "Fashion > Shoes > Sneakers",
        "Sneakers",
        "حذاء رياضي",
        (99, 899),
        ("sneaker", "running shoe", "trainer", "shoe", "cleats", "loafer"),
        ("Nike", "Adidas", "Puma", "Skechers", "New Balance", "Asics"),
        "shoe",
        priority=1,
    ),
    Category(
        "Fashion > Shoes > Sandals & Slippers",
        "Sandals",
        "صندل",
        (29, 349),
        ("sandal", "sandals", "slipper", "slippers", "flip flop", "flip flops", "slides"),
        ("Crocs", "Birkenstock", "Adidas", "Nike"),
        "shoe",
        priority=2,
    ),
    Category(
        "Fashion > Shoes > Boots",
        "Boots",
        "حذاء طويل",
        (149, 1199),
        ("boot", "boots"),
        ("Timberland", "Dr. Martens", "Clarks", "UGG"),
        "shoe",
        priority=2,
    ),
    Category(
        "Fashion > Accessories > Bags & Wallets",
        "Bag",
        "حقيبة",
        (39, 2999),
        ("bag", "backpack", "handbag", "purse", "wallet", "tote", "luggage", "suitcase", "crossbody"),
        ("Samsonite", "Michael Kors", "Guess", "Herschel", "Coach"),
        materials=("leather", "nylon"),
    ),
    Category(
        "Fashion > Accessories > Watches",
        "Watch",
        "ساعة يد",
        (79, 4999),
        ("watch", "wristwatch", "chronograph"),
        ("Casio", "Fossil", "Timex", "Seiko", "Citizen"),
    ),
    Category(
        "Fashion > Accessories > Jewellery",
        "Jewellery",
        "مجوهرات",
        (29, 2999),
        ("necklace", "bracelet", "earring", "earrings", "ring", "jewelry", "jewellery", "pendant"),
        ("Pandora", "Swarovski", "Damas", "Tiffany"),
        materials=("silver", "gold", "stainless steel"),
    ),
    Category(
        "Fashion > Accessories > Sunglasses",
        "Sunglasses",
        "نظارة شمسية",
        (49, 999),
        ("sunglasses", "eyewear", "eyeglasses", "reading glasses"),
        ("Ray-Ban", "Oakley", "Polaroid"),
        priority=2,
    ),
    Category(
        "Fashion > Kids > Baby & Kids Clothing",
        "Kids Outfit",
        "ملابس أطفال",
        (29, 249),
        ("onesie", "toddler", "baby girl", "baby boy", "kids", "girls", "boys", "romper"),
        ("Carter's", "Mothercare", "H&M", "Zara"),
        "apparel",
    ),
    Category(
        "Home & Kitchen > Kitchen > Cookware",
        "Cookware",
        "أواني طبخ",
        (49, 1499),
        ("pan", "pot", "skillet", "cookware", "wok", "dutch oven", "saucepan", "baking"),
        ("Tefal", "Pyrex", "Lodge", "Prestige", "Le Creuset"),
        materials=("stainless steel", "cast iron"),
    ),
    Category(
        "Home & Kitchen > Kitchen > Small Appliances",
        "Kitchen Appliance",
        "أجهزة مطبخ",
        (99, 2499),
        (
            "blender",
            "coffee maker",
            "air fryer",
            "toaster",
            "kettle",
            "mixer",
            "microwave",
            "juicer",
            "espresso",
            "food processor",
            "rice cooker",
        ),
        ("Philips", "Kenwood", "Tefal", "Black+Decker", "Ninja", "Nespresso", "Braun"),
        priority=2,
    ),
    Category(
        "Home & Kitchen > Kitchen > Tableware & Drinkware",
        "Tableware",
        "أدوات مائدة",
        (15, 299),
        (
            "mug",
            "cup",
            "tumbler",
            "water bottle",
            "plate",
            "glass",
            "bowl",
            "cutlery",
            "spoon",
            "fork",
            "knife",
            "dinnerware",
            "tablecloth",
            "napkin",
            "dish rack",
        ),
        ("Pyrex", "IKEA", "Stanley", "Hydro Flask", "Corelle"),
    ),
    Category(
        "Home & Kitchen > Furniture",
        "Furniture",
        "أثاث",
        (149, 4999),
        (
            "chair",
            "table",
            "desk",
            "sofa",
            "shelf",
            "bookshelf",
            "bed frame",
            "stool",
            "cabinet",
            "nightstand",
            "dresser",
        ),
        ("IKEA", "Home Centre", "Pan Emirates", "Danube Home"),
        materials=("wood", "metal"),
    ),
    Category(
        "Home & Kitchen > Bedding & Bath",
        "Bedding",
        "مفروشات",
        (39, 799),
        ("sheet", "sheets", "pillow", "blanket", "comforter", "towel", "duvet", "bedspread", "mattress"),
        ("IKEA", "Home Centre", "Pottery Barn"),
        materials=("cotton", "microfiber"),
    ),
    Category(
        "Home & Kitchen > Home Decor",
        "Home Decor",
        "ديكور منزلي",
        (25, 999),
        (
            "lamp",
            "rug",
            "carpet",
            "curtain",
            "frame",
            "candle",
            "vase",
            "decor",
            "mirror",
            "clock",
            "wall art",
            "art print",
            "sign",
            "poster",
            "ornament",
        ),
        ("IKEA", "Home Centre", "H&M Home"),
    ),
    Category(
        "Home & Kitchen > Storage & Organisation",
        "Organiser",
        "منظم تخزين",
        (19, 399),
        ("organizer", "organiser", "storage", "basket", "hanger", "hangers", "bin", "drawer"),
        ("IKEA", "Home Centre", "Sterilite"),
    ),
    Category(
        "Home & Kitchen > Cleaning",
        "Cleaning Supplies",
        "أدوات تنظيف",
        (19, 2499),
        ("vacuum", "mop", "cleaner", "broom", "detergent", "cleaning", "duster", "sponge"),
        ("Dyson", "Bissell", "Philips", "Vileda", "Dettol"),
    ),
    Category(
        "Tools & Home Improvement > Tools",
        "Tool",
        "عدد وأدوات",
        (39, 1999),
        ("drill", "screwdriver", "wrench", "tool set", "tool kit", "saw", "hammer", "pliers", "socket"),
        ("Bosch", "Makita", "DeWalt", "Black+Decker", "Stanley"),
    ),
    Category(
        "Tools & Home Improvement > Lighting & Electrical",
        "Lighting",
        "إضاءة",
        (15, 499),
        (
            "led",
            "light bulb",
            "bulb",
            "extension cord",
            "light strip",
            "string lights",
            "outlet",
            "power strip",
            "flashlight",
        ),
        ("Philips", "Osram", "Govee", "Wipro"),
    ),
    Category(
        "Beauty & Personal Care > Skin Care",
        "Skin Care",
        "عناية بالبشرة",
        (25, 599),
        (
            "moisturizer",
            "serum",
            "cream",
            "sunscreen",
            "cleanser",
            "lotion",
            "face wash",
            "toner",
            "retinol",
            "spf",
        ),
        ("The Ordinary", "CeraVe", "Nivea", "La Roche-Posay", "Garnier", "Neutrogena"),
    ),
    Category(
        "Beauty & Personal Care > Hair Care",
        "Hair Care",
        "عناية بالشعر",
        (19, 1299),
        ("shampoo", "conditioner", "hair", "hair dryer", "straightener", "curling iron", "hair oil"),
        ("L'Oreal", "Pantene", "Dyson", "Philips", "TRESemme"),
    ),
    Category(
        "Beauty & Personal Care > Makeup",
        "Makeup",
        "مكياج",
        (19, 399),
        (
            "lipstick",
            "mascara",
            "foundation",
            "eyeliner",
            "makeup",
            "nail polish",
            "eyeshadow",
            "concealer",
            "lip gloss",
            "blush",
        ),
        ("Maybelline", "MAC", "L'Oreal", "Huda Beauty", "NYX"),
    ),
    Category(
        "Beauty & Personal Care > Fragrance",
        "Perfume",
        "عطر",
        (79, 1499),
        ("perfume", "cologne", "eau de", "fragrance", "oud", "body mist", "attar"),
        ("Ajmal", "Arabian Oud", "Lattafa", "Rasasi", "Chanel", "Dior"),
    ),
    Category(
        "Beauty & Personal Care > Shaving & Grooming",
        "Grooming",
        "أدوات الحلاقة",
        (29, 899),
        ("razor", "trimmer", "shaver", "beard", "shaving", "epilator", "clipper"),
        ("Gillette", "Philips", "Braun", "Remington"),
    ),
    Category(
        "Health & Household > Vitamins & Supplements",
        "Supplement",
        "مكملات غذائية",
        (29, 399),
        (
            "vitamin",
            "vitamins",
            "supplement",
            "protein",
            "capsules",
            "gummies",
            "omega",
            "collagen",
            "probiotic",
            "whey",
        ),
        ("Nature's Bounty", "Optimum Nutrition", "Centrum", "Solgar", "Now Foods"),
    ),
    Category(
        "Health & Household > Medical Supplies",
        "Medical Supplies",
        "مستلزمات طبية",
        (19, 599),
        (
            "thermometer",
            "blood pressure",
            "face mask",
            "bandage",
            "first aid",
            "pulse oximeter",
            "brace",
            "heating pad",
            "gloves",
            "walker",
            "rollator",
            "wheelchair",
            "cane",
        ),
        ("Omron", "Braun", "3M", "Beurer"),
    ),
    Category(
        "Sports & Outdoors > Fitness",
        "Fitness Gear",
        "معدات رياضية",
        (29, 3999),
        (
            "dumbbell",
            "dumbbells",
            "yoga mat",
            "resistance band",
            "kettlebell",
            "treadmill",
            "exercise",
            "workout",
            "fitness",
            "jump rope",
        ),
        ("Decathlon", "Reebok", "Nike", "Adidas", "Bowflex"),
    ),
    Category(
        "Sports & Outdoors > Outdoor & Camping",
        "Camping Gear",
        "معدات تخييم",
        (39, 1499),
        ("tent", "sleeping bag", "camping", "hiking", "cooler", "hammock", "lantern"),
        ("Coleman", "Decathlon", "The North Face", "Columbia"),
    ),
    Category(
        "Sports & Outdoors > Cycling",
        "Bicycle",
        "دراجة هوائية",
        (99, 4999),
        ("bike", "bicycle", "helmet", "cycling", "scooter"),
        ("Giant", "Trek", "Decathlon", "Xiaomi"),
    ),
    Category(
        "Toys & Games > Toys",
        "Toy",
        "ألعاب أطفال",
        (19, 799),
        (
            "toy",
            "toys",
            "lego",
            "doll",
            "puzzle",
            "plush",
            "action figure",
            "building blocks",
            "stuffed animal",
            "rc car",
        ),
        ("LEGO", "Mattel", "Hasbro", "Fisher-Price", "Barbie"),
    ),
    Category(
        "Toys & Games > Board Games",
        "Board Game",
        "لعبة لوحية",
        (39, 299),
        ("board game", "card game", "playing cards", "chess", "monopoly", "uno", "jigsaw"),
        ("Hasbro", "Mattel"),
        priority=1,
    ),
    Category(
        "Baby > Baby Care",
        "Baby Care",
        "مستلزمات الأطفال",
        (19, 2999),
        (
            "diaper",
            "diapers",
            "baby wipes",
            "stroller",
            "pacifier",
            "baby bottle",
            "car seat",
            "crib",
            "baby monitor",
        ),
        ("Pampers", "Huggies", "Chicco", "Philips Avent", "Graco"),
        priority=1,
    ),
    Category(
        "Grocery > Beverages",
        "Beverage",
        "مشروبات",
        (9, 199),
        ("coffee", "tea", "juice", "water", "drink", "k-cup", "espresso pods", "energy drink"),
        ("Nescafe", "Lipton", "Starbucks", "Al Rawabi", "Masafi"),
    ),
    Category(
        "Grocery > Snacks & Pantry",
        "Pantry Item",
        "مواد غذائية",
        (5, 149),
        (
            "snack",
            "snacks",
            "chips",
            "chocolate",
            "candy",
            "cookies",
            "nuts",
            "sauce",
            "spice",
            "rice",
            "pasta",
            "dates",
            "honey",
            "cereal",
            "oil",
        ),
        ("Nestle", "Al Islami", "Americana", "Kellogg's", "Lay's", "Bateel"),
    ),
    Category(
        "Pet Supplies > Dogs & Cats",
        "Pet Supplies",
        "مستلزمات الحيوانات الأليفة",
        (15, 799),
        ("dog", "cat", "pet", "puppy", "kitten", "litter", "leash", "collar", "aquarium"),
        ("Purina", "Pedigree", "Whiskas", "Royal Canin"),
        priority=1,
    ),
    Category(
        "Books & Stationery > Books",
        "Book",
        "كتاب",
        (19, 199),
        (
            "book",
            "paperback",
            "hardcover",
            "novel",
            "a novel",
            "edition",
            "kindle",
            "guide",
            "anthology",
            "stories",
            "volume",
            "cookbook",
            "workbook",
            "memoir",
        ),
        ("Penguin", "HarperCollins", "Jarir", "Scholastic"),
    ),
    Category(
        "Books & Stationery > Office Supplies",
        "Office Supplies",
        "أدوات مكتبية",
        (5, 299),
        (
            "pen",
            "pencil",
            "notebook",
            "paper",
            "stapler",
            "marker",
            "sticky notes",
            "printer",
            "ink",
            "toner cartridge",
            "calculator",
            "art set",
            "crayon",
            "paint",
            "craft",
            "sketch",
        ),
        ("Pilot", "BIC", "Staedtler", "Faber-Castell", "Post-it", "HP"),
    ),
    Category(
        "Automotive > Car Accessories",
        "Car Accessory",
        "إكسسوارات السيارات",
        (19, 1999),
        (
            "car",
            "dash cam",
            "seat cover",
            "tire",
            "motor oil",
            "wiper",
            "car charger",
            "jump starter",
            "car mount",
            "truck",
            "exhaust",
            "carburetor",
            "brake",
            "engine",
            "spark plug",
            "headlight",
            "chevy",
            "ford",
            "toyota",
            "honda",
            "jeep",
            "valve cover",
        ),
        ("Michelin", "Bosch", "Garmin", "Anker", "3M"),
        priority=1,
    ),
    Category(
        "Media > Movies & Music",
        "Movie",
        "أفلام وموسيقى",
        (19, 199),
        (
            "dvd",
            "blu-ray",
            "bluray",
            "4k uhd",
            "vhs",
            "cd",
            "vinyl",
            "soundtrack",
            "complete collection",
            "complete series",
            "season",
            "greatest hits",
            "album",
        ),
        ("Warner Bros", "Universal", "Sony Pictures", "Disney"),
        priority=2,
    ),
    Category(
        "Tools & Home Improvement > Plumbing & Bath Fixtures",
        "Plumbing Fixture",
        "أدوات سباكة",
        (19, 1499),
        (
            "faucet",
            "sink",
            "drain",
            "valve",
            "pipe",
            "plumbing",
            "toilet",
            "shower",
            "shower head",
            "bidet",
            "fitting",
            "connector",
        ),
        ("Grohe", "Moen", "Kohler", "Delta", "SharkBite"),
        priority=1,
    ),
    Category(
        "Garden & Outdoor > Garden & Patio",
        "Garden Supplies",
        "مستلزمات الحديقة",
        (19, 2999),
        (
            "garden",
            "plant",
            "planter",
            "seeds",
            "hose",
            "lawn",
            "mower",
            "grill",
            "patio",
            "bird feeder",
            "fertilizer",
            "pool",
            "sprinkler",
            "weed",
        ),
        ("Gardena", "Weber", "Bosch", "Scotts", "Miracle-Gro"),
    ),
    Category(
        "Sports & Outdoors > Hunting & Fishing",
        "Hunting Gear",
        "معدات صيد",
        (29, 1999),
        (
            "fishing",
            "hunting",
            "rifle",
            "shotgun",
            "sling",
            "holster",
            "decoy",
            "deer",
            "bait",
            "lure",
            "reel",
            "fishing rod",
            "tactical",
            "airsoft",
        ),
        ("Shakespeare", "Rapala", "Daiwa", "Wildgame"),
    ),
    Category(
        "Fashion > Costumes & Party",
        "Costume",
        "أزياء تنكرية",
        (29, 399),
        ("costume", "halloween", "cosplay", "wig", "party supplies", "balloon", "mask"),
        ("Rubie's", "Disguise", "Amscan"),
        "apparel",
    ),
)

FALLBACK = Category("General > Miscellaneous", "Item", "منتج", (19, 499), ())

CATEGORIES_BY_LTREE: dict[str, Category] = {c.ltree: c for c in (*TAXONOMY, FALLBACK)}


@cache
def _keyword_patterns() -> tuple[tuple[re.Pattern[str], Category, int], ...]:
    out: list[tuple[re.Pattern[str], Category, int]] = []
    for cat in TAXONOMY:
        for kw in cat.keywords:
            # Multi-word phrases are stronger evidence than single words. Plurals match automatically.
            weight = 3 * len(kw.split()) + cat.priority
            out.append((re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?:e?s)?(?![a-z0-9])"), cat, weight))
    return tuple(out)


# Marketplace titles name the product type in their first clause ("Coffee Maker, 1.5 L, for home"); words
# after a comma/dash/"with"/"for" are mostly compatibility lists and features.
_CLAUSE_END = re.compile(r",| - | \| |\(|\[| with | for | compatible | includes ")


def classify(title: str) -> Category:
    """Best category for a product title by weighted keyword hits; FALLBACK when nothing matches."""
    t = title.lower()
    m = _CLAUSE_END.search(t)
    head_end = m.start() if m else len(t)
    scores: dict[str, int] = {}
    by_path: dict[str, Category] = {}
    for pattern, cat, weight in _keyword_patterns():
        hit = pattern.search(t)
        if hit:
            boost = 2 if hit.start() < head_end else 1
            scores[cat.path] = scores.get(cat.path, 0) + weight * boost
            by_path[cat.path] = cat
    if not scores:
        return FALLBACK
    # Deterministic tie-break on taxonomy order.
    order = {c.path: i for i, c in enumerate(TAXONOMY)}
    best = max(scores, key=lambda p: (scores[p], -order[p]))
    return by_path[best]


# --- Colours ---------------------------------------------------------------------------------------

# Canonical colour -> (Arabic name, synonyms found in titles / ESCI product_color).
COLOURS: dict[str, tuple[str, tuple[str, ...]]] = {
    "black": ("أسود", ("black", "jet", "onyx", "ebony", "midnight")),
    "white": ("أبيض", ("white", "snow", "off-white", "off white", "pearl")),
    "grey": ("رمادي", ("grey", "gray", "charcoal", "graphite", "slate", "ash", "space gray", "gunmetal")),
    "silver": ("فضي", ("silver", "chrome", "platinum")),
    "gold": ("ذهبي", ("gold", "golden", "champagne", "brass")),
    "red": ("أحمر", ("red", "burgundy", "maroon", "wine", "crimson", "scarlet", "cherry", "ruby")),
    "pink": ("وردي", ("pink", "rose", "fuchsia", "magenta", "blush", "salmon")),
    "purple": ("بنفسجي", ("purple", "violet", "lavender", "lilac", "plum", "mauve")),
    "blue": ("أزرق", ("blue", "navy", "teal", "turquoise", "aqua", "cobalt", "denim", "sky", "indigo")),
    "green": ("أخضر", ("green", "olive", "mint", "sage", "emerald", "lime", "army", "forest")),
    "yellow": ("أصفر", ("yellow", "mustard", "lemon")),
    "orange": ("برتقالي", ("orange", "coral", "peach", "rust", "tangerine")),
    "brown": (
        "بني",
        ("brown", "tan", "coffee", "chocolate", "camel", "walnut", "espresso", "mocha", "cognac"),
    ),
    "beige": ("بيج", ("beige", "cream", "ivory", "khaki", "nude", "sand", "taupe", "natural")),
    "multicolour": (
        "متعدد الألوان",
        ("multicolor", "multicolour", "multi-color", "multi", "assorted", "rainbow"),
    ),
    "clear": ("شفاف", ("clear", "transparent")),
}

_COLOUR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![a-z]){re.escape(syn)}(?![a-z])"), canon)
    for canon, (_, syns) in COLOURS.items()
    for syn in sorted(syns, key=len, reverse=True)
)


def canonical_colour(raw: str | None) -> str | None:
    """Map a free-text colour ("Space Gray", "Navy Blue/White") to the canonical lowercase English name.

    The earliest mention wins: "Black/Red" -> black.
    """
    if not raw:
        return None
    s = raw.lower()
    best: tuple[int, str] | None = None
    for pattern, canon in _COLOUR_PATTERNS:
        m = pattern.search(s)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canon)
    return best[1] if best else None


# --- Sizes -----------------------------------------------------------------------------------------

APPAREL_SIZES = ("XS", "S", "M", "L", "XL", "XXL", "3XL")
SHOE_SIZES = tuple(str(s) for s in range(36, 47))  # EU

_APPAREL_WORDS = {
    "xx-small": "XS",
    "x-small": "XS",
    "xs": "XS",
    "small": "S",
    "medium": "M",
    "large": "L",
    "x-large": "XL",
    "xl": "XL",
    "xx-large": "XXL",
    "xxl": "XXL",
    "2xl": "XXL",
    "xxx-large": "3XL",
    "3xl": "3XL",
    "xxxl": "3XL",
}
_APPAREL_RE = re.compile(
    r"(?<![a-z0-9-])(xxx-large|xx-large|x-large|xx-small|x-small|small|medium|large|xxxl|xxl|3xl|2xl|xl|xs)"
    r"(?![a-z0-9-])"
)
_SHOE_RE = re.compile(r"\bsize\s*:?\s*(\d{1,2}(?:\.5)?)\b")


def extract_size(title: str, kind: SizeKind) -> str | None:
    """Size stated in a title, normalised (apparel: XS..3XL; shoes: the number as written)."""
    t = title.lower()
    if kind == "apparel":
        m = _APPAREL_RE.search(t)
        return _APPAREL_WORDS[m.group(1)] if m else None
    if kind == "shoe":
        m = _SHOE_RE.search(t)
        return m.group(1) if m else None
    return None
