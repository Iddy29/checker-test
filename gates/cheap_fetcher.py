"""The cheap things are found by a keyword.

The fetcher is asked for a word — seeds, stickers, candles — and it
answers with the stores that have something cheap with that word, and
the thing itself: the store, the variant, and the price.

The things are put into the database of the goods, so the native gate
finds them without asking any store for its goods list again, and the
stores are put into the pool, so the cards have somewhere to go.

A word that brings nothing is written down and is not asked again for
a while: the stores that have nothing cheap with that word will not
have it tomorrow either.
"""

import asyncio
import threading
import json
import logging
import os
import sqlite3
import time
import asyncio as _asyncio
from typing import Optional

logger = logging.getLogger("cheap_fetcher")

# The place that is asked.
FETCHER = os.environ.get(
    "SHOPIFY_FETCHER",
    "https://shopify-fetcher.kamalxd.workers.dev/").rstrip("/")

# The words that are asked.  The things that are cheap and that many
# stores have: they are the things that can be sold for a little and
# that a shop can send in a small parcel.
KEYWORDS = [
    "seed", "seeds", "sticker", "stickers", "socks", "candle", "candles",
    "notebook", "keychain", "bandana", "beanie", "hat", "magnet", "pen",
    "eraser", "teabag", "teabags", "charm", "bookmark", "pin",
    # more words, for more shops: the shops that sell cheap small
    # things, and the words they are found by.
    "wallet", "cardholder", "scrunchie", "hairclip", "hairband", "cufflink",
    "tie", "bowtie", "pocketknife", "multitool", "lighter", "walletcard",
    "poster", "print", "artprint", "ornament", "coaster", "trivet",
    "mousepad", "mousemat", "deskpad", "planner", "sticky", "notepad",
    "journal", "diary", "plannerpad", "washi", "tape", "ribbon",
    "button", "buttons", "badge", "patch", "applique", "ironon",
    "sock", "leggings", "tights", "sportsocks", "crew socks",
    "coaster", "mug", "mugs", "tumbler", "waterbottle", "bottle",
    "bottleopener", "opener", "corkscrew", "wine", "coffeemug",
    "teacup", "plate", "placemat", "tablecloth", "napkin",
    "soap", "lotions", "balm", "candleholder", "diffuser", "wax",
    "incense", "potpourri", "bathbomb", "salts", "brush", "comb",
    "hairtie", "scrunchie", "headband", "gloves", "mittens", "scarf",
    "beanie", "cap", "baseballcap", "snapback", "toque",
    "leather", "belt", "wallet", "cardholder", "passport",
    "blanket", "throw", "pillow", "cushion", "fleece",
    "tote", "bag", "backpack", "pouch", "sack", "drawstring",
    "sleeve", "case", "cover", "holder", "organizer",
    "clips", "washi tape", "sticker sheet", "decal",
    "card", "greeting", "postcard", "printable", "digital",
    "puzzle", "game", "toy", "craft", "diy",
    "plants", "planter", "pot", "garden", "herbs",
    "coffee", "tea", "spice", "honey", "jam", "sauce",
    "snack", "chocolate", "candy", "gum", "jerky",
    "pet", "dog", "cat", "puppy", "kitten", "leash",
    "baby", "onesie", "bib", "pacifier", "teether",
    "school", "pencil", "pencils", "crayon", "marker", "highlighter",
    "erasers", "sharpener", "ruler", "scissors", "glue",
    "notebook refills", "planner stickers", "journaling",
    "book", "books", "novel", "cookbook", "zine",
    "vinyl", "record", "cassette", "cd", "dvd",
    "lego", "blocks", "puzzle pieces", "model",
    "bike", "cycling", "skate", "surf", "snow",
    "climb", "camp", "hike", "trail", "outdoor",
    "trophy", "medal", "award", "gift", "presents",
    "bulk", "wholesale", "reseller", "distributor", "supplies",
    "party", "birthday", "wedding", "favor", "decoration",
    "christmas", "halloween", "easter", "thanksgiving",
    # more words again, for fresh shops: the words of the small shops
    # that sell one kind of thing and nothing else.
    "figurine", "modeltrain", "minifigure", "brick", "building",
    "diecast", "collectible", "tradingcard", "comic", "manga",
    "notecard", "greetingcard", "invitation", "announcement",
    "plannerstickers", "stickersheet", "vinylsticker", "holographic",
    "resin", "sculpture", "ceramic", "pottery", "clay",
    "wooden", "bamboo", "cork", "corkboard", "leathercraft",
    "dried", "flower", "flowerarrangement", "botanical", "pressed",
    "herbal", "spiceblend", "tea", "matcha", "oolong", "chai",
    "coffee beans", "roast", "grinder", "filters", "espresso",
    "hot sauce", "sauce", "mustard", "ketchup", "chutney",
    "olive oil", "balsamic", "vinegar", "honey", "jam", "preserve",
    "granola", "cereal", "trailmix", "nuts", "seedsnack",
    "protein", "energybar", "electrolyte", "drinksmixer",
    "shaker", "waterbottle", "cooler", "coozie", "icepack",
    "yoga", "mat", "blocks", "strap", "resistance",
    "resin", "pigment", "paintbrush", "canvas", "easel",
    "watercolour", "gouache", "sketchbook", "pencilset",
    "calligraphy", "brushset", "ink", "paper",
    "journaling", "washi tape", "bulletjournal", "sticky notes",
    "planner stickers", "receipt printer", "label maker",
    "phone case", "laptop sleeve", "tablet cover", "watchband",
    "wallet case", "card holder", "coin purse", "pouch",
    "travel", "packing", "luggage tag", "passport cover",
    "cable", "charger", "adapter", "cable organizer",
    "usb hub", "sd card", "memory card", "hard drive",
    "phone stand", "car mount", "car charger", "cable tie",
    "led", "light", "lamp", "bulb", "strip light",
    "sensor", "camera", "tripod", "microphone", "ring light",
    "smart", "smartplug", "thermostat", "doorbell",
    "kitchen", "pan", "knife", "cutting board", "utensil",
    "spatula", "whisk", "grater", "peeler",
    "coffee", "grinder", "filter", "milk frother",
    "air fryer", "blender", "toaster", "kettle",
    "bottle opener", "corkscrew", "wine glass", "coaster",
    "candle holder", "tray", "table", "shelf", "organiser",
    "storage", "box", "basket", "bin", "container",
    "bed", "pillow", "blanket", "throw pillow", "cushion",
    "towel", "bath", "shower", "bathmat", "toothbrush",
    "skincare", "face", "mask", "serum", "cleanser", "moisturiser",
    "makeup", "lip", "mascara", "foundation", "brush",
    "nail", "nailart", "pressonnail", "fake nail",
    "hair", "brush", "hairdryer", "curler", "iron",
    "shaving", "razor", "aftershave",
    "baby", "nappy", "diaper", "wipe", "formula",
    "feeding", "bottle", "steriliser", "pacifier",
    "toy", "wooden toy", "puzzle", "card game", "dice",
    "board game", "craft", "diy kit", "paint kit",
    "outdoor", "tent", "sleeping bag", "backpack", "daypack",
    "cooking", "camping", "hiking", "fishing", "hunting",
    "cycling", "bike light", "bike lock", "helmet",
    "skateboard", "longboard", "surfboard", "wetsuit",
    "climbing", "carabiner", "rope", "chalk",
    "camera", "film", "instant", "polaroid", "album",
    "photography", "print", "framing", "gallery",
    "wall art", "wall decor", "poster", "tapestry",
    "mirror", "candle", "scent", "diffuser", "wax melt",
    "bookends", "book", "journal", "planner", "calendar",
    "greeting", "anniversary", "wedding", "birthday",
    "ornament", "bauble", "wreath", "garland",
    "wrapping", "ribbon", "gift tag", "card",
    "sticker", "decal", "vinyl", "holographic",
    "stationery", "letter", "pen", "greeting card",
    "craft supplies", "beads", "charms", "pendant",
]

# The window the price of a thing must be in.
MIN_PRICE = float(os.environ.get("SHOP_MIN_PRICE", "1.00") or 1.00)
MAX_PRICE = float(os.environ.get("SHOP_MAX_PRICE", "23.00") or 23.00)

# How many times a word that brings nothing is asked again.
WORD_TTL = float(os.environ.get("CHEAP_WORD_TTL", "21600") or 21600)

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "variants.db")
_WORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cheap_words.json")


def _the_words_asked():
    """The words that were asked, and when."""
    try:
        with open(_WORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _remember_the_word(word: str, found: int) -> None:
    data = _the_words_asked()
    data[word] = {"at": time.time(), "found": found}
    try:
        with open(_WORD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the words could not be written down: %s", e)


def _the_price(text) -> Optional[float]:
    """The price out of the words of the fetcher: '1.99 USD'."""
    if text is None:
        return None
    raw = str(text).strip()
    for part in raw.replace(",", " ").split():
        try:
            value = float(part)
        except (TypeError, ValueError):
            continue
        return value
    return None


def _the_store(site: str) -> str:
    """The store, in the words a session understands."""
    s = (site or "").strip().strip('"').lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip("/")
    if "/" in s:
        s = s.split("/", 1)[0]
    return s


def _open_the_database():
    """The database of the goods, made if it is not there."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS variants(
                       site TEXT, variant_id TEXT, price REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sites(
                       site TEXT PRIMARY KEY, cheapest_vid TEXT,
                       cheapest_price REAL)""")
    try:
        # an older table without the key is given one
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sites)")]
        if "site" in cols:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS sites_site "
                         "ON sites(site)")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS variants_site ON variants(site)")
    conn.commit()
    return conn


def _put_the_things(conn, rows) -> int:
    """The things are put in, and the cheapest of each store with them."""
    added = 0
    for site, variant_id, price in rows:
        try:
            conn.execute("INSERT INTO variants(site, variant_id, price) VALUES(?,?,?)",
                         (site, variant_id, price))
            added += 1
        except Exception:
            pass
    for site, variant_id, price in rows:
        try:
            cur = conn.execute("SELECT cheapest_price FROM sites WHERE site=?",
                               (site,))
            row = cur.fetchone()
            if row is None or price < (row[0] or 1e9):
                conn.execute(
                    "INSERT OR REPLACE INTO sites(site, cheapest_vid, "
                    "cheapest_price) VALUES(?,?,?)",
                    (site, variant_id, price))
        except Exception:
            pass
    conn.commit()
    return added


async def _ask(word: str, timeout: float = 90.0) -> list:
    """The fetcher is asked for a word."""
    import aiohttp
    url = "%s/?keyword=%s" % (FETCHER, word)
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    logger.info("the fetcher said %s for the word %s", r.status, word)
                    return []
                data = await r.json(content_type=None)
    except Exception as e:
        logger.info("the fetcher would not answer for the word %s: %s", word,
                    str(e)[:80])
        return []
    return data if isinstance(data, list) else []


async def find_cheap(words=None, add_to_pool: bool = True) -> dict:
    """The cheap things are found, and put in the database."""
    words = list(words or KEYWORDS)
    asked = _the_words_asked()
    now = time.time()
    todo = []
    for word in words:
        entry = asked.get(word)
        if entry and (now - float(entry.get("at") or 0)) < WORD_TTL \
                and not int(entry.get("found") or 0):
            continue                      # the word brings nothing
        todo.append(word)

    rows = []
    stores = []
    for word in todo:
        found = await _ask(word)
        good = 0
        for one in found:
            if not isinstance(one, dict):
                continue
            site = _the_store(one.get("site") or "")
            variant_id = str(one.get("variant_id") or "").strip()
            price = _the_price(one.get("price"))
            if not site or not variant_id or price is None:
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            rows.append((site, variant_id, price))
            if site not in stores:
                stores.append(site)
            good += 1
        _remember_the_word(word, good)
        logger.info("the word %s brought %d cheap things", word, good)

    conn = _open_the_database()
    try:
        added = _put_the_things(conn, rows)
    finally:
        conn.close()

    if add_to_pool and stores:
        _add_to_the_pool(stores)

    return {"words": len(todo), "things": added, "stores": len(stores)}


def _add_to_the_pool(stores) -> None:
    """The stores are put in the pool of the validator, at the front."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "shopify-api", "site.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return
    added = []
    for site in stores:
        if site not in lines:
            lines.append(site)
            added.append(site)
    if not added:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("%d stores were put in the pool", len(added))
    except Exception as e:
        logger.warning("the pool could not be written: %s", e)

async def probe_the_pool(words=None, add_to_pool: bool = False) -> dict:
    """The shops of the pool are asked, and the ones that can take a
    card are told apart from the ones that cannot.

    The shop is asked for its goods list, for its basket and for the
    price of the order, and the card is not put in.  A shop that gets
    that far can take a card, and a shop that does not is remembered as
    one that cannot, so a card does not wait for it.
    """
    import asyncio as _asyncio
    from . import shopify_native_gate as _gate
    from . import shopify_native_flow as _flow

    shops = _gate._the_pool_of_shops()
    limit = int(os.environ.get("PROBE_LIMIT", "260") or 260)
    shops = shops[:limit]
    workers = int(os.environ.get("PROBE_WORKERS", "16") or 16)
    good = []
    bad = []
    sem = asyncio.Semaphore(workers)

    def _r(response, message="", product="", price="0.00", charged=False,
           site_error=False):
        return {"Response": response, "Message": message or response,
                "code": response, "decline_code": response,
                "Gateway": "Shopify Payments", "Price": price,
                "Proxy": "Live", "Status": not site_error,
                "Gate": "Shopify Payments", "CC": "", "Product": product,
                "Site": "", "Charged": "False", "Approved": "False",
                "Time": "0s", "Retry": site_error}

    async def _one(site):
        async with sem:
            try:
                profile = _flow.pick_browser_profile()
                # Each shop is asked from a place of its own, for the
                # shops count the asking per place.
                where = _gate._the_next_place()
                out = await _flow._one_shop("5178057497043781", "03", "27",
                                            "247", site, where, 15.0, profile,
                                            _r)
            except Exception:
                out = {"Response": "SITE_ERROR"}
            code = str(out.get("Response") or "").upper()
            said = str(out.get("Message") or "")
            kind = str(out.get("Captcha") or "")
            return site, code, said, kind

    started = time.time()
    results = await asyncio.gather(*[_one(s) for s in shops],
                                     return_exceptions=True)
    for one in results:
        if isinstance(one, Exception):
            continue
        site, code, said, kind = one
        # The question of the probe: does this shop send to us?
        if "NO_DELIVERY_STRATEGY_AVAILABLE" in said.upper():
            # it does not send to our address, and no card will ever be
            # put in it, so it is out for good
            bad.append(site)
            try:
                _gate._remember_the_no_ship_store(site)
            except Exception:
                pass
        elif code in ("CARD_DECLINED", "ORDER_PLACED", "3DS_REQUIRED",
                      "EXPIRED_CARD", "INVALID_CARD", "INVALID_CVC",
                      "INSUFFICIENT_FUNDS", "SHOP_ORDER_ERROR",
                      "PROCESSING_ERROR", "UNKNOWN"):
            # it got as far as the payment, so it sends to us
            good.append(site)
        elif code == "CAPTCHA_REQUIRED":
            # The kind of the captcha is remembered: the free one is
            # answered by our own solver, and the shop is kept.  A shop
            # with another kind is a shop a card does not go to.
            try:
                _gate._remember_the_captcha(site, kind)
            except Exception:
                pass
            if kind == "hcaptcha" or not kind:
                good.append(site)
            else:
                bad.append(site)
        else:
            bad.append(site)
    for site in bad:
        try:
            _gate._remember_the_bad_store(site)
        except Exception:
            pass
    if good and add_to_pool:
        _add_to_the_pool(good)
    if good:
        # The shops that can take a card are written down, for a card
        # goes straight to one of them.
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "working_stores.json")
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            for site in good:
                key = site.replace("https://", "").replace("http://", "")
                data[key] = int(data.get(key) or 0) + 1
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception as e:
            logger.info("the shops that work were not written down: %s", e)
    return {"asked": len(shops), "good": len(good), "bad": len(bad),
            "seconds": round(time.time() - started, 1)}

async def prepare_the_pool() -> dict:
    """The machine is made ready: the places are tested, and the shops
    of the pool are looked at.

    The places that do not answer are left alone, for a card that goes
    to one of them waits for nothing.  The shops that cannot take a
    card are left alone too, for the same reason.
    """
    from . import shopify_native_gate as _gate
    out = {}
    try:
        out["places"] = await _gate.check_the_places()
    except Exception as e:
        logger.info("the places could not be tested: %s", str(e)[:80])
    try:
        out["shops"] = await probe_the_pool(words=None)
    except Exception as e:
        logger.info("the pool could not be looked at: %s", str(e)[:80])
    return out

async def warm_the_goods_lists(words=None) -> dict:
    """The goods lists of the shops are read, and put in the database.

    A card that goes into a shop does not read the goods list of that
    shop: the list is read here, once, and the card finds it ready.  The
    shops of the validator service are the ones that are read, for they
    are the ones that answer.
    """
    import sqlite3 as _sq
    from . import shopify_native_gate as _gate

    shops = []
    for path in (_gate._API_SITES_FILE, _gate._SITES_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and line not in shops:
                        shops.append(line)
        except Exception:
            pass
    limit = int(os.environ.get("WARM_LIMIT", "80") or 80)
    shops = shops[:limit]
    if not shops:
        return {"shops": 0, "warmed": 0}

    db = _gate._DB_PATH
    if not os.path.exists(db):
        return {"shops": len(shops), "warmed": 0}
    conn = _sq.connect(db, timeout=20)
    conn.row_factory = _sq.Row
    try:
        have = {r["site"] for r in conn.execute(
            "SELECT DISTINCT site FROM variants")}
    except Exception:
        have = set()

    todo = []
    for site in shops:
        key = _gate._normalize_site_key(site)
        if key in have:
            continue
        todo.append(site)

    sem = asyncio.Semaphore(8)
    rows = []
    lock = threading.Lock()

    async def _one(site):
        async with sem:
            got = None
            try:
                got, why = await _gate.fetch_products(site, None)
            except Exception:
                got = None
            if not got:
                return
            key = _gate._normalize_site_key(site)
            with lock:
                rows.append((key, str(got.get("variant_id")),
                             float(got.get("price") or 0)))

    await asyncio.gather(*[_one(s) for s in todo], return_exceptions=True)

    added = 0
    try:
        for site, variant_id, price in rows:
            if not variant_id or price <= 0:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO variants (site, variant_id, price) "
                "VALUES (?, ?, ?)", (site, variant_id, price))
            added += 1
        conn.commit()
    except Exception as e:
        logger.info("the goods lists could not be written down: %s", e)
    finally:
        conn.close()
    return {"shops": len(shops), "read": len(todo), "warmed": added}
