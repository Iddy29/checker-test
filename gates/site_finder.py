import re
import asyncio
import time
import logging
import random
import os
import base64
from urllib.parse import urlparse, quote_plus, unquote, parse_qs
from typing import Dict, Any, List, Optional, Callable

import httpx
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

logger = logging.getLogger("site_finder")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]

PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy.txt")


def _fmt_proxy(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return None


def _load_proxies() -> List[str]:
    """The proxies the search uses.

    proxy.txt is the admin's own list.  When it is empty the user's
    proxies are used instead -- otherwise every search leaves from one
    address and the engines rate limit the whole scan.
    """
    proxies = []
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE) as f:
                lines = [l.strip() for l in f if l.strip()]
            for raw in lines:
                p = _fmt_proxy(raw)
                if p:
                    proxies.append(p)
    except Exception:
        pass
    if proxies:
        return proxies

    try:
        import json as _json
        from gateways import get_user_proxy_list
        raws = get_user_proxy_list("1214399247") or []
        for raw in raws:
            p = _fmt_proxy(raw)
            if p:
                proxies.append(p)
    except Exception:
        pass
    return proxies


def _get_random_proxy() -> Optional[str]:
    proxies = _load_proxies()
    return random.choice(proxies) if proxies else None

_NICHES = [
    "church", "school", "university", "hospital", "museum", "library",
    "animal rescue", "food bank", "homeless shelter", "youth center",
    "community center", "arts center", "theater", "orchestra", "ballet",
    "wildlife", "conservation", "environmental", "climate", "ocean",
    "children", "education", "scholarship", "mentorship", "tutoring",
    "health", "cancer", "diabetes", "mental health", "veterans",
    "disaster relief", "humanitarian", "refugee", "immigrant",
    "senior care", "elder care", "hospice", "palliative",
    "sports", "athletics", "soccer", "basketball", "swimming",
    "music", "choir", "band", "piano", "guitar",
    "gardening", "farming", "agriculture", "sustainable",
    "literacy", "reading", "books", "writing",
    "dance", "yoga", "fitness", "martial arts",
    "science", "stem", "robotics", "coding", "technology",
    "pregnancy", "adoption", "foster care", "childcare",
    "disability", "autism", "blindness", "deaf",
    "housing", "affordable", "shelter", "transitional",
    "legal aid", "justice", "civil rights", "equality",
    "addiction", "recovery", "substance abuse", "rehab",
    "meditation", "spiritual", "worship", "ministry",
    "historical society", "heritage", "preservation",
    "botanical garden", "zoo", "aquarium", "planetarium",
    "fire department", "ems", "ambulance", "search rescue",
    "rotary", "lions club", "kiwanis", "elks",
    "girl scouts", "boy scouts", "4h", "ymca", "ywca",
]

_ACTIONS = [
    "donate", "donation", "give", "support", "contribute",
    "checkout", "payment", "pay", "buy", "purchase",
    "subscribe", "membership", "join", "register", "sign up",
    "fund", "sponsor", "pledge", "tithe", "offering",
]

_TLDS = [".org", ".com", ".net", ".edu", ".church", ".charity", ".foundation"]



_REGIONS = [
    "", "USA", "UK", "Canada", "Australia", "New Zealand",
    "California", "Texas", "Florida", "New York", "Ohio",
    "London", "Toronto", "Sydney", "India", "Europe",
]

# The /st gate needs a WooCommerce shop with the WooCommerce Stripe
# plugin -- those are the shops it can ask to confirm a setup intent.
# The plugin's own asset folder is what the search engines index.
_WOOSTRIPE_TEMPLATES = [
    'inurl:wp-content/plugins/woocommerce-gateway-stripe',
    '"woocommerce-gateway-stripe" {niche}',
    'inurl:plugins/woocommerce-gateway-stripe {niche}',
    '"woocommerce-gateway-stripe" {action}',
    'inurl:wp-content/plugins/woocommerce-gateway-stripe {niche} {region}',
    '"woocommerce-gateway-stripe" {niche} donate',
    'inurl:wp-content/plugins/woocommerce-gateway-stripe {niche} {action}',
    '"wc_stripe_upe_params" {niche}',
    '"createAndConfirmSetupIntent" {niche}',
    'inurl:wc-ajax=wc_stripe {niche}',
    'inurl:plugins/woocommerce-gateway-stripe {niche} checkout',
    '"woocommerce-gateway-stripe" {niche} online',
]

_BROAD_DORK_TEMPLATES = [
    'inurl:{action} "{gateway_term}"',
    '"{gateway_term}" "{action}" inurl:{tld}',
    '"powered by {gateway_term}" {niche}',
    '"payments by {gateway_term}"',
    'inurl:donate "{gateway_term}"',
    'inurl:checkout "{gateway_term}"',
    '"{gateway_term}" site:{tld} {action}',
    '"{gateway_term}" {niche} {action}',
    'inurl:payment "{gateway_term}"',
    '"{gateway_term}" {niche} donate',
    'site:{tld} inurl:{action} "{gateway_term}"',
    '"{gateway_term}" {action} site:{tld}',
]

# The engines mangle a query with several quoted terms or an inurl:
# operator -- they answer with something else entirely.  So the dorks
# are plain words and the check on the page does the rest.
_AUTHORIZENET_TEMPLATES = [
    'authorizenet payment form',
    'authorizenet credit card payment',
    'authorizenet make a payment',
    'authorizenet hosted payment form',
    'authorizenet billing payment form',
    'authorizenet donate payment',
    'accept js authorizenet payment form',
]

_STRIPE_DONATION_TEMPLATES = [
    'stripe donation form',
    'donate with stripe credit card',
    'stripe donation checkout',
    'one time donation stripe',
    'stripe donate now credit card',
    'stripe donation amount',
]

_SHOPIFY_TEMPLATES = [
    "site:myshopify.com {niche}",
    "site:myshopify.com {niche} shop",
    "myshopify.com {niche} buy online",
    "myshopify.com {niche} collections",
    '"{niche}" site:myshopify.com',
    "myshopify.com {niche} products {region}",
]

_PAYPAL_TEMPLATES = [
    # The search engines mangle a query with several quoted terms or an
    # inurl: operator -- they answer with something else entirely.  So
    # the dorks are plain words and the check on the page does the rest.
    'paypal donate button',
    'paypal donation form',
    'paypal donate now',
    'paypal smart buttons',
    'paypal donate wordpress',
    'paypal checkout charity',
    'paypal donate foundation',
    'paypal give online',
    'paypal donation nonprofit',
    'paypal donate church',
]


_BRAINTREE_TEMPLATES = [
    # a donation page with Braintree hosted fields (the Drupal /
    # Springboard shape the gates can use)
    '"braintree-hosted-fields" donate',
    '"braintree checkout" donation',
    '"pay with credit card" "braintree" donation',
    'inurl:donate "braintree"',
    'inurl:donation "braintree" credit card',
    'inurl:give "braintree"',
    'intitle:donate "braintree"',
    '"payment_method_nonce" braintree',
    '"clientToken" "production_" donate',
    '"braintree" "springboard" donation',
    # a WooCommerce shop with the Braintree plugin
    '"wc-braintree" checkout',
    '"woocommerce-gateway-braintree"',
    '"braintree_credit_card" checkout',
    '"braintree" "woocommerce" "add-to-cart"',
    'inurl:checkout "braintree" woocommerce',
    '"client_token_nonce" braintree',
    '"braintree" "my-account" "add-payment-method"',
    '"payment_method_nonce" "braintree" shop',
    '"braintreegateway.com" "add-to-cart"',
    '"braintree" "cart" "checkout" "credit card"',
]

_DORK_TEMPLATES = [
    "inurl:{action} {niche} {gateway_term}",
    "inurl:{action} site:{tld} {gateway_term}",
    "{niche} {action} online {gateway_term}",
    "{niche} {action} {gateway_term} {region}",
    "inurl:{action} {niche} credit card {region}",
    '"{action} now" {niche} {gateway_term}',
    "site:{tld} {niche} {action} {gateway_term}",
    "{niche} nonprofit {action} online {gateway_term}",
    '"{action}" {gateway_term} {niche} secure',
    "inurl:checkout {niche} {gateway_term}",
    "{niche} foundation {action} {gateway_term}",
    "{niche} charity {action} online credit card",
    "inurl:give {niche} {gateway_term} {region}",
    "{action} {niche} {gateway_term} secure online",
    '"{niche}" "{action}" {gateway_term}',
    "inurl:payment {niche} {gateway_term}",
]

_GATEWAY_TERMS = {
    "stripe": ["stripe", "credit card", "debit card", "card payment", "secure payment"],
    "wcstripe": ["woocommerce-gateway-stripe", "wc_stripe", "woocommerce stripe",
                 "wc_stripe_upe_params", "createAndConfirmSetupIntent"],
    "braintree": [
        "braintree payment", "braintree credit card",
        "wc-braintree", "braintree-hosted-fields",
        "braintree checkout", "powered by braintree",
        "credit card donate", "credit card donation online",
        "donate credit card", "pay with card",
    ],
    "paypal": ["paypal", "paypal checkout", "paypal credit card", "-site:paypal.com"],
    "square": ["square", "square payment", "square checkout"],
    "authorize.net": ["authorize.net", "authorize payment", "authorizenet"],
    "adyen": ["adyen", "adyen checkout", "adyen payment"],
    "razorpay": ["razorpay", "razorpay checkout", "razorpay payment"],
    "worldpay": ["worldpay", "worldpay payment"],
    "cybersource": ["cybersource", "cybersource payment", "flex microform"],
    "shopify": ["myshopify.com", "shopify store", "shopify checkout"],
    "sagepay": ["sagepay", "opayo", "sagepay payment"],
    "klarna": ["klarna", "klarna checkout", "klarna payment"],
    "mollie": ["mollie", "mollie payment"],
    "payu": ["payu", "payu payment", "payubiz"],
    "paystack": ["paystack", "paystack payment"],
    "elavon": ["elavon", "converge payment"],
    "heartland": ["heartland", "heartland payment"],
}

# Short, broad queries: the search engines return far more for these and
# the verification filters the noise out.  A deep scan needs both kinds.
_USED_DORKS_CACHE = {}
_MAX_CACHE_PER_GW = 200


def _generate_dorks(gateway: str, count: int = 12) -> List[str]:
    gw = gateway.lower()
    terms = _GATEWAY_TERMS.get(gw, [gateway])
    if gw in ("authorize.net", "authorizenet", "authnet"):
        templates = _AUTHORIZENET_TEMPLATES
    elif gw in ("stripe", "stripedonation", "stripe_donation"):
        templates = _STRIPE_DONATION_TEMPLATES + _DORK_TEMPLATES
    elif gw == "shopify":
        templates = _SHOPIFY_TEMPLATES
    elif gw == "braintree":
        templates = _BRAINTREE_TEMPLATES + _DORK_TEMPLATES
    elif gw == "paypal":
        templates = _PAYPAL_TEMPLATES + _DORK_TEMPLATES
    elif gw == "wcstripe":
        templates = _WOOSTRIPE_TEMPLATES + _DORK_TEMPLATES
    else:
        templates = _DORK_TEMPLATES

    cache_key = gw
    if cache_key not in _USED_DORKS_CACHE:
        _USED_DORKS_CACHE[cache_key] = set()
    used = _USED_DORKS_CACHE[cache_key]

    if len(used) > _MAX_CACHE_PER_GW:
        used.clear()

    dorks = []
    attempts = 0
    max_attempts = count * 8
    broad = _BROAD_DORK_TEMPLATES

    while len(dorks) < count and attempts < max_attempts:
        attempts += 1
        # two out of every three queries are the broad kind
        if broad and random.random() < 0.65:
            tmpl = random.choice(broad)
        else:
            tmpl = random.choice(templates)
        niche = random.choice(_NICHES)
        action = random.choice(_ACTIONS)
        tld = random.choice(_TLDS)
        region = random.choice(_REGIONS)
        gw_term = random.choice(terms)

        dork = tmpl.format(
            niche=niche,
            action=action,
            tld=tld,
            region=region,
            gateway_term=gw_term,
        ).strip()

        while "  " in dork:
            dork = dork.replace("  ", " ")

        if dork not in used:
            used.add(dork)
            dorks.append(dork)

    random.shuffle(dorks)
    return dorks


GATEWAY_DORKS = {}

GATEWAY_KEYWORDS = {
    "stripe": r"js\.stripe\.com|stripe\.com/v|pk_live_|pk_test_|stripe\.js|Stripe\(|stripe-checkout",
    # a WooCommerce shop running the WooCommerce Stripe plugin -- the only
    # kind of shop the /st setup-intent gate can use
    "wcstripe": r"woocommerce-gateway-stripe|wc_stripe_upe_params|wc_stripe_params|"
                r"createAndConfirmSetupIntent|wc-ajax=wc_stripe",
    "braintree": r"braintreegateway\.com|braintree\.js|braintree-web|braintree\.client|wc-braintree|braintree/vendor|braintree_credit_card|payment_method_nonce",
    "paypal": r"paypal\.com/sdk/js|paypalobjects|paypal\.js|paypal-checkout|"
              r"paypal\.Buttons|paypal-button|data-paypal|"
              r"paypal\.com/cgi-bin/webscr|paypal\.com/donate|"
              r"paypal\.com/paypal|PPAK|"
              r"paypal\.com/signin",
    "square": r"squareup\.com|square\.js|js\.squareup\.com|web-payments-sdk|sq0idp|sq0csp",
    "authorize.net": r"authorize\.net|authorizenet|accept\.js|AcceptUI",
    "adyen": r"adyen\.com|adyen\.js|checkoutshopper|adyen\.encrypt|adyen-cse",
    "razorpay": r"razorpay\.com|checkout\.razorpay\.com|Razorpay\(",
    "worldpay": r"worldpay\.com|worldpay",
    "cybersource": r"cybersource\.com|flex-microform",
    "shopify": r"cdn\.shopify\.com|shopify-pay|shop\.js|shopify-payment",
    "sagepay": r"sagepay\.com|opayo\.com",
    "klarna": r"klarna\.com|klarna\.js|x-klarnacdn|klarna-payments",
    "mollie": r"mollie\.com|js\.mollie\.com",
    "payu": r"payu\.in|payu\.com|payubiz",
    "paystack": r"paystack\.com|js\.paystack\.co",
    "elavon": r"elavon\.com|converge\.com",
    "heartland": r"heartland\.us|heartlandpaymentsystems",
}

IGNORE_DOMAINS = {
    "google.com", "bing.com", "yahoo.com", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "linkedin.com", "reddit.com",
    "wikipedia.org", "amazon.com", "ebay.com", "apple.com", "microsoft.com",
    "github.com", "stackoverflow.com", "quora.com", "pinterest.com",
    "tiktok.com", "netflix.com", "spotify.com", "twitch.tv",
    "stripe.com", "paypal.com", "braintree.com", "braintreepayments.com", "braintreegateway.com", "squareup.com",
    "authorize.net", "adyen.com", "razorpay.com", "worldpay.com",
    "shopify.com", "wordpress.org", "wix.com", "squarespace.com",
    "medium.com", "blogspot.com", "tumblr.com", "npmjs.com",
    "discord.com", "telegram.org", "whatsapp.com",
    "w3schools.com", "developer.mozilla.org", "docs.google.com",
    "support.google.com", "play.google.com", "apps.apple.com",
    "duckduckgo.com", "ecosia.org", "startpage.com", "brave.com",
    "trustpilot.com", "capterra.com", "g2.com", "crunchbase.com",
    "donorbox.org", "givewp.com", "wpcharitable.com", "checkoutpage.com",
    "paymattic.com", "zeffy.com", "merchantmaverick.com", "givebutter.com",
    "soapboxengage.com", "springly.org", "glueup.com", "charitycharge.com",
    "wise.com", "biddingowl.com", "firespring.com", "donorperfect.com",
    "givewell.org", "fundly.com", "classy.org", "networkforgood.com",
    "chargebee.com", "recurly.com", "chargify.com", "zuora.com",
    "github.io", "gitlab.io", "netlify.app", "vercel.app", "heroku.com",
    "readthedocs.io", "gitbook.io", "confluence.com", "notion.so",
    "zapier.com", "ifttt.com", "hubspot.com", "mailchimp.com",
    "nerdwallet.com", "investopedia.com", "forbes.com", "techcrunch.com",
    "entrepreneur.com", "inc.com", "businessinsider.com",
    "pcmag.com", "cnet.com", "zdnet.com", "theverge.com",
    "blog.hubspot.com", "blog.stripe.com", "blog.paypal.com",
    "docs.stripe.com", "developer.paypal.com",
    "salesforce.com", "salesforce-sites.com",
}


def _get_ua():
    return random.choice(USER_AGENTS)


def _extract_real_url(href: str) -> Optional[str]:
    if not href:
        return None
    if href.startswith("/url?") or "google.com/url?" in href:
        params = parse_qs(urlparse(href).query)
        real_list = params.get("q", params.get("url", []))
        real = real_list[0] if real_list else None
        if real and real.startswith("http"):
            return real
        return None
    if "duckduckgo.com/l/?" in href:
        params = parse_qs(urlparse(href).query)
        real_list = params.get("uddg", [])
        real = real_list[0] if real_list else None
        if real:
            return unquote(real)
        return None
    if "bing.com/ck/a?" in href:
        params = parse_qs(urlparse(href).query)
        u_list = params.get("u", [])
        if u_list:
            encoded = u_list[0]
            if encoded.startswith("a1"):
                try:
                    decoded = base64.b64decode(encoded[2:]).decode('utf-8', errors='ignore')
                    if decoded.startswith("http"):
                        return decoded
                except Exception:
                    pass
        return None
    if href.startswith("http"):
        return href
    return None


def _cffi_search(engine: str, query: str, proxy: Optional[str] = None, num: int = 20) -> List[str]:
    """Search the engine, straight from this machine.

    Measured: the search engines answer in about 0.3 s directly, while
    the same query through one of the proxies waits for the whole
    timeout and then gives nothing.  So the direct route is the only
    one that is asked.

    The proxy route is not a fallback either: bing through a proxy
    answers with results for a different question altogether (the
    quotes and the operator are dropped and the words of the query are
    searched on their own), so the hits are of no use.  It was the
    reason the scan found nothing but dictionary and weather sites.
    """
    return _cffi_search_inner(engine, query, None, num)


def _cffi_search_inner(engine: str, query: str, proxy: Optional[str] = None, num: int = 20) -> List[str]:
    urls = []
    proxies = {"https": proxy, "http": proxy} if proxy else None

    try:
        if engine == "duckduckgo":
            r = cffi_requests.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "b": ""},
                impersonate="chrome",
                proxies=proxies,
                timeout=20,
            )
            if r.status_code != 200:
                return urls
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", class_="result__a", href=True):
                href = a["href"]
                real = _extract_real_url(href)
                if real:
                    urls.append(real)
                elif href.startswith("http") and "duckduckgo" not in href:
                    urls.append(href)
            for a in soup.find_all("a", class_="result__url", href=True):
                href = a["href"]
                if href.startswith("http") and "duckduckgo" not in href:
                    urls.append(href)
                elif not href.startswith("http"):
                    urls.append("https://" + href.strip())

        elif engine == "google":
            r = cffi_requests.get(
                f"https://www.google.com/search?q={quote_plus(query)}&num={num}",
                impersonate="chrome",
                proxies=proxies,
                timeout=15,
            )
            if r.status_code != 200:
                return urls
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                real = _extract_real_url(href)
                if real and "google" not in urlparse(real).netloc:
                    urls.append(real)

        elif engine == "bing":
            # Bing is the only engine that still answers, so it is asked
            # for several pages instead of just the first one.
            # Two pages are enough: the deep scan gets its depth from
            # the many dorks, not from paging, and every extra page is
            # another request that Bing may answer slowly.
            pages = 2
            for page in range(pages):
                first = 1 + page * 10
                try:
                    r = cffi_requests.get(
                        f"https://www.bing.com/search?q={quote_plus(query)}"
                        f"&count={num}&first={first}",
                        impersonate="chrome",
                        proxies=proxies,
                        timeout=15,
                    )
                except Exception:
                    break
                if r.status_code != 200:
                    break
                text = r.text
                if "captcha" in text.lower():
                    break
                soup = BeautifulSoup(text, "html.parser")
                found = 0
                for li in soup.find_all("li", class_="b_algo"):
                    a = li.find("a", href=True)
                    if a:
                        href = a["href"]
                        real = _extract_real_url(href)
                        if real:
                            urls.append(real)
                            found += 1
                    cite = li.find("cite")
                    if cite:
                        cite_text = cite.get_text().strip()
                        if cite_text.startswith("http"):
                            urls.append(cite_text.split(" ")[0])
                if found == 0:
                    break

        elif engine == "yahoo":
            # yahoo gives a different set of results for every question,
            # which is what bing stopped doing for this address.
            try:
                r = cffi_requests.get(
                    f"https://search.yahoo.com/search?p={quote_plus(query)}&n=30",
                    impersonate="chrome",
                    proxies=proxies,
                    timeout=20,
                )
            except Exception:
                r = None
            if r is not None and r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = (a.get("href") or "").strip()
                    if "r.search.yahoo.com" in href:
                        m = re.search(r"RU=(.+?)/RK=", href)
                        if m:
                            from urllib.parse import unquote as _unq
                            urls.append(_unq(m.group(1)))
                    elif href.startswith("http") and "yahoo.com" not in href:
                        urls.append(href)

        elif engine == "marginalia":
            # a small independent index -- good for the sites the big
            # engines do not rank
            try:
                r = cffi_requests.get(
                    f"https://search.marginalia.nu/search?query={quote_plus(query)}",
                    impersonate="chrome",
                    proxies=proxies,
                    timeout=20,
                )
            except Exception:
                r = None
            if r is not None and r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = (a.get("href") or "").strip()
                    if href.startswith("http") and "marginalia" not in href:
                        real = _extract_real_url(href)
                        if real:
                            urls.append(real)

        elif engine == "brave":
            for offset in [0, 10]:
                r = cffi_requests.get(
                    f"https://search.brave.com/search?q={quote_plus(query)}&source=web&offset={offset}",
                    impersonate="chrome",
                    proxies=proxies,
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("http") and "brave.com" not in href:
                        parsed = urlparse(href)
                        if parsed.netloc and "." in parsed.netloc:
                            urls.append(href)
                if len(urls) >= num:
                    break

        elif engine == "startpage":
            r = cffi_requests.get(
                f"https://www.startpage.com/do/search?q={quote_plus(query)}",
                impersonate="chrome",
                proxies=proxies,
                timeout=15,
            )
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("http") and "startpage" not in href:
                        parsed = urlparse(href)
                        if parsed.netloc and "." in parsed.netloc:
                            urls.append(href)

    except Exception as e:
        logger.debug(f"{engine} search error: {e}")

    return urls[:num]


# The searches are IO bound, so they get their own pool with more
# workers than the default one.
_SEARCH_POOL = None
SEARCH_WORKERS = int(os.environ.get("FINDER_SEARCH_WORKERS", "96"))


def _search_pool():
    global _SEARCH_POOL
    if _SEARCH_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _SEARCH_POOL = ThreadPoolExecutor(max_workers=SEARCH_WORKERS,
                                          thread_name_prefix="finder")
    return _SEARCH_POOL


async def _async_cffi_search(engine: str, query: str, proxy: Optional[str] = None, num: int = 20) -> List[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_search_pool(), _cffi_search, engine,
                                      query, proxy, num)


def _is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = (parsed.hostname or '').lower()
        if not hostname:
            return False
        blocked = ['localhost', '127.0.0.1', '0.0.0.0', '::1']
        if hostname in blocked:
            return False
        ip_match = re.match(r'^(\d+)\.(\d+)\.(\d+)\.(\d+)$', hostname)
        if ip_match:
            octets = [int(o) for o in ip_match.groups()]
            if octets[0] == 10:
                return False
            if octets[0] == 172 and 16 <= octets[1] <= 31:
                return False
            if octets[0] == 192 and octets[1] == 168:
                return False
            if octets[0] == 169 and octets[1] == 254:
                return False
            if octets[0] == 0 or octets[0] >= 224:
                return False
        return True
    except Exception:
        return False


def _filter_urls(urls: List[str]) -> List[str]:
    seen_domains = set()
    filtered = []
    for url in urls:
        try:
            if not _is_safe_url(url):
                continue
            parsed = urlparse(url)
            domain = parsed.netloc.lower().replace('www.', '', 1)
            if not domain:
                continue
            root = '.'.join(domain.split('.')[-2:])
            if root in IGNORE_DOMAINS or domain in IGNORE_DOMAINS:
                continue
            if any(domain.endswith('.' + ign) or domain == ign for ign in IGNORE_DOMAINS):
                continue
            path_lower = parsed.path.lower()
            if any(kw in path_lower for kw in ['/blog/', '/docs/', '/tutorial', '/documentation/', '/guide/', '/how-to', '/article/', '/learn/', '/reference/', '/api-reference/', '/support/', '/help/', '/faq/', '/resources/', '/knowledge/', '/hc/', '/en-us/articles/', '/white', '/review', '/comparison']):
                continue
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            filtered.append(url)
        except Exception:
            continue
    return filtered


PK_PATTERN = re.compile(r'pk_(live|test)_[A-Za-z0-9]{10,}')


RZP_KEY_PATTERN = re.compile(r'rzp_(?:live|test)_[A-Za-z0-9]{8,}')
RZP_LINK_PATTERN = re.compile(r'https?://(?:razorpay\.me|pages\.razorpay\.com)/[^\s"\'<>]+')


def _extract_keys_from_html(html: str, gateway: str) -> List[str]:
    keys = []
    if gateway == 'stripe':
        full_matches = PK_PATTERN.finditer(html)
        seen = set()
        for m in full_matches:
            pk = m.group(0)
            if pk not in seen:
                seen.add(pk)
                keys.append(pk)
    elif gateway in ('authorize.net', 'authorizenet', 'authnet'):
        # The merchant's page carries the two keys the gate needs: the
        # api login id and the client key of the Authorize.net account.
        seen = set()
        for m in re.finditer(r'data-apiLoginID="([^"]+)"', html, re.I):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                keys.append(m.group(1))
        for m in re.finditer(r'data-clientKey="([^"]+)"', html, re.I):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                keys.append(m.group(1))
    elif gateway == 'razorpay':
        seen = set()
        for m in RZP_LINK_PATTERN.finditer(html):
            link = m.group(0).rstrip('/')
            if link not in seen:
                seen.add(link)
                keys.append(link)
        for m in RZP_KEY_PATTERN.finditer(html):
            rk = m.group(0)
            if rk not in seen:
                seen.add(rk)
                keys.append(rk)
    return keys


async def _verify_gateway(client: httpx.AsyncClient, url: str, gateway: str) -> Optional[Dict[str, Any]]:
    pattern = GATEWAY_KEYWORDS.get(gateway.lower())
    if not pattern:
        return None
    try:
        headers = {"User-Agent": _get_ua(), "Accept": "text/html,application/xhtml+xml"}
        r = await client.get(url, timeout=6, follow_redirects=True, headers=headers)
        if r.status_code != 200:
            return None
        html = r.text

        def _make_result(found_url, evidence, found_html):
            keys = _extract_keys_from_html(found_html, gateway)
            result = {'url': found_url, 'evidence': evidence, 'domain': urlparse(url).netloc}
            if keys:
                result['keys'] = keys
            if gateway == 'razorpay':
                rzp_links = RZP_LINK_PATTERN.findall(found_html)
                if rzp_links:
                    result['rzp_link'] = rzp_links[0].rstrip('/')
                    if 'keys' not in result:
                        result['keys'] = []
                    for rl in rzp_links:
                        rl = rl.rstrip('/')
                        if rl not in result['keys']:
                            result['keys'].append(rl)
            return result

        # The script tags, the links and the forms are read with
        # patterns.  The html parser is slow, and it ran in the event
        # loop, so it made the checks one after another instead of all
        # at once.
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)', html, re.IGNORECASE):
            if re.search(pattern, src, re.IGNORECASE):
                return _make_result(url, 'script', html)
        for content in re.findall(r'<script[^>]*>(.*?)</script>', html,
                                  re.IGNORECASE | re.DOTALL):
            if re.search(pattern, content[:5000], re.IGNORECASE):
                return _make_result(url, 'inline', html)
        if re.search(pattern, html[:50000], re.IGNORECASE):
            return _make_result(url, 'html', html)

        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        subpage_urls = set()

        common_paths = ['/donate', '/donation', '/give', '/checkout', '/donate-now',
                        '/payment', '/payments', '/support/donate', '/cart',
                        '/membership', '/subscribe']
        for path in common_paths:
            subpage_urls.add(base + path)

        checkout_keywords = re.compile(
            r'donat|checkout|payment|pay\b|buy|purchase|order|subscribe|cart|give|contrib',
            re.IGNORECASE
        )
        for href, inner in re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html,
                re.IGNORECASE | re.DOTALL):
            href = href.strip()
            text = re.sub(r'<[^>]+>', ' ', inner).strip()
            if checkout_keywords.search(text) or checkout_keywords.search(href):
                if href.startswith('http') and urlparse(href).netloc == urlparse(url).netloc:
                    subpage_urls.add(href.split('?')[0].split('#')[0])
                elif href.startswith('/'):
                    subpage_urls.add(base + href.split('?')[0].split('#')[0])
                elif not href.startswith(('http', '#', 'mailto:', 'tel:', 'javascript:')):
                    parent = url.rsplit('/', 1)[0] if '/' in urlparse(url).path.lstrip('/') else base
                    subpage_urls.add(parent + '/' + href.split('?')[0].split('#')[0])
        for action in re.findall(r'<form[^>]+action=["\']([^"\']+)', html, re.IGNORECASE):
            action = action.strip()
            if action and not action.startswith(('http', '#', 'mailto:', 'javascript:')):
                parent = url.rsplit('/', 1)[0] if '/' in urlparse(url).path.lstrip('/') else base
                subpage_urls.add(parent + '/' + action.split('?')[0].split('#')[0])

        subpage_urls.discard(url)
        subpages = list(subpage_urls)[:6]

        async def _check_sub(sub_url):
            try:
                r2 = await client.get(sub_url, timeout=6,
                                      follow_redirects=True, headers=headers)
                if r2.status_code == 200 and re.search(pattern, r2.text[:80000], re.IGNORECASE):
                    return _make_result(sub_url, 'subpage', r2.text)
            except Exception:
                pass
            return None

        # the sub pages are checked at the same time: one after another
        # they were the slowest part of the whole scan
        for res in await asyncio.gather(*[_check_sub(u) for u in subpages]):
            if res:
                return res

    except Exception as e:
        logger.debug(f"Verify error for {url}: {e}")
    return None


_BRAINTREE_SEEDS = [
    "https://www.aclu.org/give/now",
    "https://help.rescue.org/donate/refugees-welcome",
    "https://www.tea-and-coffee.com/checkout/",
    "https://give.salvationarmyusa.org/give/164006",
    "https://www.nrdc.org/get-involved/ways-give",
    "https://www.aspca.org/ways-to-give",
    "https://www.ewg.org/donate",
    "https://www.sierraclub.org/sierra/donate",
    "https://support.edf.org/give",
    "https://www.greenpeace.org/usa/donate/",
    "https://www.hrw.org/donate",
    "https://www.amnesty.org/en/donate/",
    "https://www.msf.org/donate",
    "https://www.doctorswithoutborders.org/donate",
    "https://www.npr.org/donations/support",
    "https://www.pbs.org/donate/",
    "https://www.woundedwarriorproject.org/donate",
    "https://www.stjude.org/donate/donate-to-st-jude.html",
    "https://support.worldwildlife.org/site/Donation2",
    "https://www.heifer.org/give/donate.html",
    "https://www.lls.org/ways-to-give",
    "https://www.crs.org/donate",
    "https://www.feedthechildren.org/donate",
    "https://www.specialolympics.org/get-involved/donate",
    "https://www.marchofdimes.org/ways-to-give",
    "https://www.operationsmile.org/donate",
    "https://www.oxfamamerica.org/donate",
    "https://www.nature.org/en-us/get-involved/how-to-help/donate-to-our-mission/",
]


async def find_sites(
    gateway: str,
    max_results: int = 10,
    progress_callback: Optional[Callable] = None,
    **kwargs,
) -> Dict[str, Any]:
    start = time.time()
    gateway_lower = gateway.lower()

    deep = kwargs.get("deep", True)
    dork_count = 70 if gateway_lower in ("braintree", "shopify") else 45
    if not deep:
        # a quick scan is a quick scan
        dork_count = 24
    dorks = _generate_dorks(gateway_lower, count=dork_count)

    if progress_callback:
        await progress_callback(f"Searching for {gateway} sites...")

    proxies = _load_proxies()
    random.shuffle(proxies)

    # Only the engines that answer are asked.  Measured: bing gives 30
    # urls in 1.5 s, marginalia a few in 1 s, while duckduckgo waits for
    # its full 20 s timeout and returns nothing, and google, brave and
    # startpage answer with an empty page.  Asking them only made the
    # scan slower.
    # Bing answers the same ten results for every query from this
    # address, so yahoo is asked as well: it gives a different set for
    # every question.
    engines = ["bing", "yahoo", "marginalia"]
    search_tasks = []
    pi = 0

    for i, dork in enumerate(dorks):
        # Every dork goes to Bing (with deep pagination) AND to the
        # other engine, so a dork is not lost when one answers nothing.
        p = proxies[pi % len(proxies)] if proxies else None
        pi += 1
        search_tasks.append(_async_cffi_search("bing", dork, p, 30))

        eng = engines[1 + (i % (len(engines) - 1))]
        p2 = proxies[pi % len(proxies)] if proxies else None
        pi += 1
        search_tasks.append(_async_cffi_search(eng, dork, p2, 20))

    for i in range(min(4, len(dorks))):
        backup_eng = engines[i % len(engines)]
        p = proxies[pi % len(proxies)] if proxies else None
        pi += 1
        search_tasks.append(_async_cffi_search(backup_eng, dorks[i], p, 25))

    logger.info(f"Launching {len(search_tasks)} search tasks with {len(proxies)} proxies")
    _t_search = time.time()
    raw_results = await asyncio.gather(*search_tasks, return_exceptions=True)
    logger.info(f"the searches took {round(time.time() - _t_search, 1)}s")

    all_urls = []
    if gateway_lower == "braintree":
        seed_sample = random.sample(_BRAINTREE_SEEDS, min(len(_BRAINTREE_SEEDS), 15))
        all_urls.extend(seed_sample)

    for result in raw_results:
        if isinstance(result, list):
            all_urls.extend(result)

    logger.info(f"Search for {gateway}: {len(all_urls)} raw URLs from {len(search_tasks)} tasks")

    filtered = _filter_urls(all_urls)
    logger.info(f"After filtering: {len(filtered)} unique candidate sites")

    if progress_callback:
        await progress_callback(f"Found {len(filtered)} candidates, verifying {gateway}...")

    _t_verify = time.time()
    async with httpx.AsyncClient(
        headers={"User-Agent": _get_ua()},
        follow_redirects=True,
        verify=True,
        timeout=httpx.Timeout(5),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    ) as verify_client:
        verified = []
        seen_domains = set()
        # The checks are IO bound, so many of them run at once: the wide
        # batches are what a deep scan is for.
        batch_size = 128
        verify_limit = 700 if gateway_lower == "braintree" else 400
        if not deep:
            verify_limit = min(verify_limit, 120)
        # The keyword check is what finds the stores, so it gets most of
        # the time -- but a scan that takes ten minutes is no use to
        # anyone, and the second half of the candidates rarely adds
        # anything.
        deadline = time.time() + (100.0 if deep else 40.0)
        pages_fetched = 0
        for i in range(0, min(len(filtered), verify_limit), batch_size):
            if time.time() > deadline:
                logger.info(f"finder: time budget reached after {i} candidates")
                break
            batch = filtered[i:i + batch_size]
            tasks = [_verify_gateway(verify_client, url, gateway_lower) for url in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, dict) and r['domain'] not in seen_domains:
                    seen_domains.add(r['domain'])
                    verified.append(r)
            if progress_callback and verified:
                await progress_callback(
                    f"Verified {len(verified)} sites from {i + len(batch)} candidates..."
                )
            # keep going: a deeper scan collects the best sites, not just
            # the first few that answer

    logger.info(f"the checking took {round(time.time() - _t_verify, 1)}s")
    elapsed = round(time.time() - start, 2)
    return {
        'gateway': gateway,
        'searched': len(filtered),
        'found': verified[:max_results],
        'count': min(len(verified), max_results),
        'elapsed': elapsed,
        'depth': {
            'dorks': len(dorks),
            'engines': len(engines),
            'raw_urls': len(all_urls),
            'candidates': len(filtered),
            'verified_total': len(verified),
            'deep': deep,
        },
    }


def format_finder_result(data: Dict[str, Any], username: str = "") -> str:
    gateway = data.get('gateway', '?')
    searched = data.get('searched', 0)
    found = data.get('found', [])
    count = data.get('count', 0)
    elapsed = data.get('elapsed', 0)
    checked_by = f"@{username}" if username else "Anonymous"
    depth = data.get('depth') or {}
    depth_line = ""
    if depth:
        mode = "deep" if depth.get("deep") else "quick"
        depth_line = (f"\U0001f50e **Scan:** {mode} | {depth.get('dorks', 0)} dorks x "
                      f"{depth.get('engines', 0)} engines | {depth.get('raw_urls', 0)} raw urls | "
                      f"{depth.get('candidates', 0)} candidates verified "
                      f"({depth.get('verified_total', 0)} hit)\n")

    if count == 0:
        return (
            f"\u250f\u2501\u2501\u2501\u2501\u300e Site Finder \u300f\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f50d **Gateway:** {gateway.title()}\n"
            f"\U0001f4ca **Searched:** {searched} sites\n"
            f"\u274c **Found:** 0 sites\n\n"
            f"{depth_line}"
            f"No sites with **{gateway.title()}** gateway found.\n"
            f"Try a different gateway.\n\n"
            f"\u23f1 **Time:** {elapsed}s\n"
            f"\U0001f464 **By:** {checked_by}\n\n"
            f"\u2517\u2501\u2501\u2501\u2501\u300e I-NETTZ CHECKER \u300f\u2501\u2501\u2501\u2501"
        )

    sites_text = ""
    all_keys = []
    for i, site in enumerate(found, 1):
        domain = site.get('domain', '?')
        url = site.get('url', '?')
        keys = site.get('keys', [])
        sites_text += f"**{i}.** `{domain}`\n"
        sites_text += f"   \u2514\u2500 {url}\n"
        if keys:
            for pk in keys:
                sites_text += f"   \U0001f511 `{pk}`\n"
                if pk not in all_keys:
                    all_keys.append(pk)
        sites_text += "\n"

    keys_section = ""
    if all_keys:
        pk_keys = [k for k in all_keys if k.startswith("pk_")]
        rzp_links = [k for k in all_keys if "razorpay.me" in k or "pages.razorpay.com" in k]
        rzp_keys = [k for k in all_keys if k.startswith("rzp_")]

        if pk_keys:
            keys_section += "\U0001f511 **Stripe PK Keys:**\n"
            for pk in pk_keys:
                keys_section += f"  `/addpk {pk}`\n"
            keys_section += "\n"

        if rzp_links:
            keys_section += "\U0001f511 **Razorpay Links:**\n"
            for link in rzp_links:
                keys_section += f"  `/addrzsite {link}`\n"
            keys_section += "\n"

        if rzp_keys:
            keys_section += "\U0001f511 **Razorpay Keys:**\n"
            for rk in rzp_keys:
                keys_section += f"  `{rk}`\n"
            keys_section += "\n"

    return (
        f"\u250f\u2501\u2501\u2501\u2501\u300e Site Finder \u300f\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f50d **Gateway:** {gateway.title()}\n"
        f"\U0001f4ca **Searched:** {searched} sites\n"
        f"\u2705 **Found:** {count} verified sites\n\n"
        f"{depth_line}"
        f"{sites_text}"
        f"{keys_section}"
        f"\u23f1 **Time:** {elapsed}s\n"
        f"\U0001f464 **By:** {checked_by}\n\n"
        f"\u2517\u2501\u2501\u2501\u2501\u300e I-NETTZ CHECKER \u300f\u2501\u2501\u2501\u2501"
    )


SUPPORTED_GATEWAYS = sorted(set(list(GATEWAY_DORKS.keys()) + list(GATEWAY_KEYWORDS.keys())))
