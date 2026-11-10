"""Find more shops to look at.

The shops that are already known are the beginning.  More come from
the places that list shops, and from the shops themselves, which name
their sister shops.
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
from urllib.parse import urlparse, quote_plus

logger = logging.getLogger("shop_seeds")

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
# the folder the whole thing sits in, where the shop lists are kept
ROOT = os.path.dirname(os.path.dirname(BOT))
OUT = os.path.join(BOT, "shopify_seeds.txt")

THE_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

# The searches that find shops.
THE_SEARCHES = [
    'site:myshopify.com',
    'inurl:myshopify.com "add to cart"',
    '"myshopify.com/products"',
    '"shopify.com/products" "add to cart" -site:pinterest.com',
    '"$1.00" "add to cart" shopify',
    '"$2.00" "add to cart" shopify',
    '"$3.00" "add to cart" shopify',
    '"$4.00" "add to cart" shopify',
    '"$5.00" "add to cart" shopify',
    '"$1.00" "sale price" shopify',
    '"$2.99" "add to cart" shopify',
    '"free shipping" "add to cart" "myshopify.com"',
]

# The places that list shops.
THE_LISTS = [
    "https://www.similarweb.com/website/shopify.com/top-websites/",
    "https://www.similarweb.com/website/myshopify.com/top-websites/",
]

# The places the searches are made from.
THE_ENGINES = [
    "https://html.duckduckgo.com/html/?q=",
    "https://lite.duckduckgo.com/lite/?q=",
    "https://search.marcia.cc/search?q=",
    "https://www.mojeek.com/search?q=",
]


def _headers():
    return {
        "User-Agent": random.choice(THE_AGENTS),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _hosts_in(text):
    """The shop names in a page of words."""
    found = set()
    for match in re.finditer(r"\b([a-z0-9][a-z0-9-]{2,40})\.myshopify\.com\b", text, re.I):
        found.add(match.group(1).lower() + ".myshopify.com")
    return found


def _links_in(text):
    """The shop names that are written as links."""
    found = set()
    for match in re.finditer(r"https?://([a-z0-9][a-z0-9-]{2,40}\.(?:myshopify\.com|com|net|co|shop|store|io))", text, re.I):
        host = match.group(1).lower()
        if host.endswith(".myshopify.com"):
            found.add(host)
    return found


async def main():
    import random
    from curl_cffi import requests as cffi_requests
    global random
    random = __import__("random")

    known = set()
    for path in (OUT, os.path.join(ROOT, "site.txt"),
                 os.path.join(ROOT, "hitchk-app", "shopify-api", "site.txt"),
                 os.path.join(BOT, "site.txt")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        known.add(line.lower())
        except Exception:
            pass
    print("the shops already known: %d" % len(known))

    session = cffi_requests.AsyncSession(impersonate="chrome136")
    fresh = set()

    # the places that list shops
    for url in THE_LISTS:
        try:
            resp = await session.get(url, headers=_headers(), timeout=25)
            if resp.status_code == 200:
                got = _hosts_in(resp.text)
                print("   %-56s %d shops" % (url[:56], len(got)))
                fresh |= got
        except Exception:
            print("   %-56s it broke" % url[:56])

    # the searches, from every place that answers
    for query in THE_SEARCHES:
        for engine in THE_ENGINES:
            try:
                resp = await session.get(engine + quote_plus(query),
                                        headers=_headers(), timeout=25)
                if resp.status_code != 200:
                    continue
                got = _hosts_in(resp.text)
                if got:
                    print("   the search %-34s %-22s %d shops" % (
                        query[:34], engine.split("/")[2][:22], len(got)))
                    fresh |= got
                    break
            except Exception:
                continue

    # the shops that are already known may name more
    for host in list(known)[:30]:
        try:
            resp = await session.get("https://" + host + "/", headers=_headers(), timeout=15)
            if resp.status_code == 200:
                fresh |= _hosts_in(resp.text)
        except Exception:
            pass

    fresh -= known
    print()
    print("the new shops: %d" % len(fresh))

    with open(OUT, "w", encoding="utf-8") as f:
        for host in sorted(known):
            f.write(host + "\n")
        for host in sorted(fresh):
            f.write(host + "\n")
    print("written to: %s (%d lines)" % (OUT, len(known) + len(fresh)))


if __name__ == "__main__":
    asyncio.run(main())
