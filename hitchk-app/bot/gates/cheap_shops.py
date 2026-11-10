"""Find the shops whose goods are cheap.

The card gate wants a shop with something to buy at a low price: the
lower the price, the more of the card will go through.  So the shops
that are wanted are the ones with goods under a few money.

What is written is the list of shops, one to a line, for the shopify
gate to use.
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
from urllib.parse import urlparse

logger = logging.getLogger("cheap_shops")

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
# the folder the whole thing sits in, where the shop lists are kept
ROOT = os.path.dirname(os.path.dirname(BOT))

# Where the list is written.
OUT = os.path.join(BOT, "shopify_cheap_sites.txt")

# The prices that are wanted, and how many of each a shop should have.
THE_PRICES = [1.0, 2.0, 3.0, 4.0, 5.0]
THE_MAXIMUM = 5.0

# Where the shops to start from come from.
THE_SEEDS = os.path.join(BOT, "shopify_seeds.txt")

# The page of a shop that lists its goods with the prices.
PRODUCTS_PAGE = "/products.json?limit=250"

# The shops are looked at with these, so a shop does not see them all
# coming from one place.
THE_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

# A shop's name, from the places it is written.
NAME_PATTERNS = [
    re.compile(r'"name"\s*:\s*"([^"]{2,60})"'),
    re.compile(r"<title[^>]*>([^<]{2,80}?)</title>", re.I),
    re.compile(r'<meta[^>]+property="og:site_name"[^>]+content="([^"]{2,60})"', re.I),
    re.compile(r'<meta[^>]+content="([^"]{2,60})"[^>]+property="og:site_name"', re.I),
]


def _headers():
    return {
        "User-Agent": random.choice(THE_AGENTS),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _name_of(html, fallback):
    """The name of the shop, so the list says what it is."""
    for pattern in NAME_PATTERNS:
        found = pattern.search(html or "")
        if found:
            name = " ".join(found.group(1).split())
            if name and not name.lower().startswith(("shopify", "home", "online store")):
                return name[:70]
    return fallback


def _prices_of(products):
    """Every price in the shop's goods, with how many are at each.

    The real goods are counted apart from the add-ons, so a shop that
    sells nothing but add-ons is still known to sell something.
    """
    counts = {}
    add_on_counts = {}
    everything = []
    for product in products or []:
        add_on = _is_an_add_on(product.get("title"))
        variants = product.get("variants") or []
        if not variants and product.get("price"):
            variants = [{"price": product.get("price")}]
        for variant in variants:
            raw = variant.get("price")
            if raw is None:
                continue
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            everything.append(price)
            # the price is put in the band it belongs to
            for wanted in THE_PRICES:
                if abs(price - wanted) < 0.01:
                    if add_on:
                        add_on_counts[wanted] = add_on_counts.get(wanted, 0) + 1
                    else:
                        counts[wanted] = counts.get(wanted, 0) + 1
                    break
    return counts, everything, add_on_counts


# The pool of addresses.  A shop counts the asking per address, and
# every shop asked from the same place is what makes the shops say no.
PROXY_FILES = (
    "/root/hitchecker/proxy.txt",
    os.path.join(ROOT, "proxy.txt"),
    os.path.join(ROOT, "hitchk-app", "bot", "proxy.txt"),
)


def _the_addresses():
    """Every address the machine holds, in the form the library wants."""
    out = []
    for path in PROXY_FILES:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    parts = raw.split(":")
                    if len(parts) == 4:
                        host, port, user, pwd = parts
                        out.append("http://%s:%s@%s:%s" % (user, pwd, host, port))
                    elif len(parts) == 2 and "." in parts[0]:
                        out.append("http://%s:%s" % (parts[0], parts[1]))
        except Exception:
            continue
        if out:
            break
    return out


def _is_a_shop(hostname):
    """Does this look like a shop at all."""
    if not hostname or "." not in hostname:
        return False
    if hostname.endswith(".myshopify.com"):
        return True
    # a name with a word in it, and nothing that says it is not a shop
    bad = ("facebook.", "instagram.", "twitter.", "pinterest.", "youtube.",
           "google.", "amazon.", "ebay.", "reddit.", "linkedin.", "tiktok.")
    low = hostname.lower()
    if any(low.startswith(b) or ("." + b) in low for b in bad):
        return False
    return True


async def _look_at_one(session, base, sem, address=None):
    """What this shop has, and whether it is worth keeping.

    A shop gets tired of being asked, so each shop is asked one at a
    time, and a pause is made between the tries.
    """
    urls = [
        base.rstrip("/") + PRODUCTS_PAGE,
        base.rstrip("/") + "/collections/all/products.json?limit=250",
    ]
    for url in urls:
        try:
            async with sem:
                resp = await session.get(url, headers=_headers(), timeout=20,
                                        proxy=address)
        except Exception:
            return None
        products = _read_the_answer(resp)
        if products:
            found = _the_goods_of(products)
            if found:
                found["host"] = urlparse(base).netloc
                return found
        # the shop said no, so the next way of asking is tried, but
        # not straight away
        await asyncio.sleep(1.5)
    return None


def _read_the_answer(resp):
    """The goods out of the answer, or nothing."""
    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(resp, "status", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    if status != 200:
        return None
    body = getattr(resp, "content", None)
    if body is None:
        body = getattr(resp, "text", "") or ""
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("products") or []


# The names of the things a shop puts beside its goods: a donation, a
# return, a bit of protection, a tip.  They are cheap and they are
# sold, but they are not the goods, so they are passed over.
ADD_ON_WORDS = (
    "donation", "donate", "free return", "package protection",
    "shipping protection", "protection plan", "warranty", "gift card",
    "giftcard", "tip", "gratuity", "optional", "add-on", "addon",
    "insurance", "savings plan", "installment",
)


def _is_an_add_on(title):
    """True when the thing is an add-on and not a real good."""
    low = str(title or "").strip().lower()
    if not low:
        return False
    return any(word in low for word in ADD_ON_WORDS)


def _the_goods_of(products):
    """What the shop has that is wanted."""
    counts, everything, add_on_counts = _prices_of(products)
    if not everything:
        return None
    cheapest = min(everything)
    if cheapest > THE_MAXIMUM:
        return None
    have = [p for p in THE_PRICES if counts.get(p)]
    # A shop that sells nothing but add-ons still sells something, so
    # those are counted when there is no real good to sell.
    add_ons = False
    if not have:
        have = [p for p in THE_PRICES if add_on_counts.get(p)]
        counts = dict(add_on_counts)
        add_ons = True
    if not have:
        return None
    return {
        "cheapest": cheapest,
        "the_prices": have,
        "the_counts": counts,
        "the_products": len(products),
        "the_add_ons": add_ons,
    }


async def main():
    import random
    from curl_cffi import requests as cffi_requests

    global random
    random = __import__("random")

    seeds = []
    for path in (THE_SEEDS,
                 os.path.join(ROOT, "site.txt"),
                 os.path.join(ROOT, "hitchk-app", "shopify-api", "site.txt"),
                 os.path.join(BOT, "site.txt")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        seeds.append(line)
        except Exception:
            pass
    # the same shop is not looked at twice
    uniq = []
    seen = set()
    for seed in seeds:
        host = seed.strip()
        if host.startswith("http"):
            host = urlparse(host).netloc
        if not host or host in seen:
            continue
        seen.add(host)
        uniq.append(host)
    seeds = uniq
    print("the shops to start from: %d" % len(seeds))

    # the shops that are already known
    known = set()
    try:
        with open(OUT, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    known.add(line.strip())
    except Exception:
        pass

    # One at a time, with a pause: a shop that is asked too much says
    # no and keeps saying no, and then the shop is of no use.
    sem = asyncio.Semaphore(2)
    found = []
    seen = set()
    addresses = _the_addresses()
    print("the addresses to ask from: %d" % len(addresses))

    tasks = []
    for seed in seeds:
        host = seed.strip()
        if host.startswith("http"):
            host = urlparse(host).netloc
        if not _is_a_shop(host) or host in seen:
            continue
        seen.add(host)
        # every shop is asked from a different address, and each one
        # has its own session
        address = random.choice(addresses) if addresses else None
        tasks.append((host, asyncio.ensure_future(
            _look_at_one(cffi_requests.AsyncSession(impersonate="chrome136"),
                          "https://" + host, sem, address))))

    done = 0
    for host, task in tasks:
        done += 1
        try:
            result = await task
        except Exception:
            result = None
        if result and result["host"] not in known:
            found.append(result)
            print("   %-38s the cheapest %6.2f | the prices: %s" % (
                result["host"][:38], result["cheapest"],
                ",".join("%d$:%d" % (int(p), result["the_counts"].get(p, 0))
                          for p in result["the_prices"])))
        # a pause, so a shop is not asked again straight away
        await asyncio.sleep(1.5)
        if done % 25 == 0:
            print("   ... %d of %d looked at, %d good" % (done, len(tasks), len(found)))

    print()
    print("the shops with cheap goods: %d" % len(found))
    if not found:
        print("nothing new was found; the list that is there is kept")
        return

    # the ones with the most of the wanted prices come first, so the
    # gate tries the best ones first
    found.sort(key=lambda r: (-len(r["the_prices"]), r["cheapest"]))

    # The shops that were already known are kept: a shop that was
    # asked while it was busy is not a shop that is bad, and a run that
    # finds nothing new must not empty a good list.
    known = []
    try:
        with open(OUT, encoding="utf-8", errors="replace") as f:
            for line in f:
                host = line.strip()
                if host and host not in known:
                    known.append(host)
    except Exception:
        pass

    the_list = list(known)
    for one in found:
        if one["host"] not in the_list:
            the_list.append(one["host"])

    with open(OUT, "w", encoding="utf-8") as f:
        for host in the_list:
            f.write(host + "\n")
    print("written to: %s (%d shops, %d of them new)" % (
        OUT, len(the_list), len(the_list) - len(known)))

    # the detail, so there is a record of what each one has
    detail = []
    try:
        with open(OUT + ".json", encoding="utf-8") as f:
            detail = json.load(f)
    except Exception:
        detail = []
    have = {one["host"]: one for one in detail if isinstance(one, dict)}
    for one in found:
        have[one["host"]] = one
    with open(OUT + ".json", "w", encoding="utf-8") as f:
        json.dump(list(have.values()), f, indent=1)
    print("the detail written to: %s.json" % OUT)

    print()
    print("the best ones:")
    for one in found[:15]:
        print("   %-40s the cheapest %6.2f | %s" % (
            one["host"][:40], one["cheapest"],
            ", ".join("%d$ x%d" % (int(p), one["the_counts"].get(p, 0))
                      for p in one["the_prices"])))


if __name__ == "__main__":
    asyncio.run(main())
