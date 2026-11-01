"""The captcha stripe asks for.

Stripe does not put its captcha on the page the buyer is on.  It puts
it in an invisible frame that comes from stripe's own content delivery
network, and the captcha is told the host of that frame, not the host
of the page.  A captcha solved on the page is a different captcha, and
it is answered with a picture instead of an answer.

So the captcha is solved where stripe put it: the page is opened, the
frame stripe loaded is found, and the answer is asked for inside it,
with the key and the request data that came with the page.
"""

# The frame that holds the captcha.  Stripe puts several frames on its
# own content network and only one of them holds the captcha, so the
# one is found by looking for the captcha itself and not by the host.
STRIPE_CAPTCHA_FRAME = "HCaptchaInvisible"

# The frame that holds the captcha of the payment intent.  It is a
# different frame from the one above: the one above is stripe's own
# captcha for the page, and this one is the challenge the payment
# intent is waiting for.  They have different keys, and an answer for
# one is not an answer for the other.
STRIPE_INTENT_FRAME = "hcaptcha-inner"

# The frame stripe's script sits in.  It is the one that says which host
# the captcha is for.
STRIPE_CAPTCHA_SHELL = "hcaptcha-invisible"

# The host the captcha is told.  It is read from the frame when the
# page is open, and this is what it is when it cannot be read.
STRIPE_CAPTCHA_HOST = "b.stripecdn.com"

# The code that asks for the answer inside the frame.  The captcha
# hands it over by itself: there is no box to click.
ASK_THE_CAPTCHA = """
    (args) => {
        const [sitekey, rqdata, host] = args;
        const keep = (t) => {
            if (t && typeof t === 'object') {
                window.__lcToken = t.response || '';
                window.__lcEkey = t.ekey || '';
            } else {
                window.__lcToken = t || '';
            }
        };
        try {
            if (!window.hcaptcha) { return 'no captcha here'; }
            // The captcha has to be put up before it can be asked for an
            // answer.  It is put up on the host it is told, which is the
            // host of the frame it is in.
            const box = document.createElement('div');
            box.style.position = 'fixed';
            box.style.left = '0px';
            box.style.top = '0px';
            box.style.width = '1px';
            box.style.height = '1px';
            box.style.opacity = '0.01';
            box.style.zIndex = '2147483647';
            document.body.appendChild(box);
            const opts = {
                sitekey: sitekey,
                size: 'invisible',
                callback: keep,
                'error-callback': (e) => { window.__lcError = String(e); }
            };
            let id = null;
            try {
                id = window.hcaptcha.render(box, opts);
            } catch (e) {
                window.__lcError = 'the render failed: ' + e;
                return 'the render failed: ' + e;
            }
            window.__lcId = id;

            // The request data goes in its own place when the answer is
            // asked for, and not inside the widget: this is the way
            // stripe's own script asks for it, and a request data put
            // somewhere else makes an answer that stripe will not take.
            let p = null;
            try {
                p = window.hcaptcha.execute(id, { async: true, rqdata: rqdata });
            } catch (e) {
                window.__lcError = 'execute threw: ' + e;
            }
            if (p && p.then) {
                p.then(keep).catch((e) => {
                    window.__lcError = String(e);
                    // the other way of asking, for when the first one
                    // is not the one this captcha takes
                    try {
                        const p2 = window.hcaptcha.execute(id, {
                            async: true,
                            enterprisePayload: { rqdata: rqdata }
                        });
                        if (p2 && p2.then) {
                            p2.then(keep).catch((e2) => { window.__lcError = String(e2); });
                        }
                    } catch (e2) { }
                });
                return 'asked';
            }
            try {
                const p2 = window.hcaptcha.execute(id, {
                    async: true,
                    enterprisePayload: { rqdata: rqdata }
                });
                if (p2 && p2.then) {
                    p2.then(keep).catch((e2) => { window.__lcError = String(e2); });
                    return 'asked';
                }
            } catch (e) { }
            return 'execute gave nothing';
        } catch (e) {
            window.__lcError = String(e);
            return 'threw: ' + e;
        }
    }
"""

# The code that reads the answer out of the frame.
READ_THE_ANSWER = """
() => ({
    token: window.__lcToken || '',
    ekey: window.__lcEkey || '',
    error: window.__lcError || '',
})
"""


def find_the_captcha_frame(page):
    """The frame that holds the captcha, and the host it is for.

    Stripe's script sits in one frame and the captcha itself is in
    another, on stripe's content network.  The captcha is told the host
    of the second one, so that is the host the answer is asked for
    under.
    """
    shell = None
    for frame in page.frames:
        url = frame.url or ""
        if STRIPE_CAPTCHA_FRAME in url:
            # the frame that holds the captcha.  The captcha is told the
            # host of the frame it is in, so the host is read from the
            # frame itself.
            return frame, frame_host(url) or host_of_the_page(frame)
        if STRIPE_CAPTCHA_SHELL in url:
            shell = frame
    if shell is not None:
        return shell, frame_host(shell.url) or ""
    return None, ""


def host_of_the_page(frame):
    """The host the frame came from, when the frame does not say."""
    try:
        from urllib.parse import urlparse
        return urlparse(frame.url or "").netloc
    except Exception:
        return ""


def intent_challenge(page):
    """The key, the request data and the address of the challenge that
    the payment intent is waiting for.

    Stripe puts the challenge of the payment intent in a frame of its
    own, and the address of that frame carries everything about it: the
    key, the request data, and the address the answer is handed to.
    That frame is on stripe's own site, and the challenge belongs to
    that frame and not to the page.

    The captcha of the page is a different captcha, with a different
    key, and an answer for it is refused by the payment intent.
    """
    from urllib.parse import unquote

    for frame in page.frames:
        url = frame.url or ""
        if STRIPE_INTENT_FRAME not in url:
            continue
        tail = url.split("#", 1)[-1]
        found = {}
        for part in tail.split("&"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            found[name] = unquote(value)
        site_key = found.get("sitekey") or ""
        rqdata = found.get("rqdata") or ""
        if not site_key or not rqdata:
            continue
        verify = found.get("verifyUrl") or ""
        if verify and not verify.startswith("/"):
            verify = "/" + verify
        return {
            "frame": frame,
            "site_key": site_key,
            "rqdata": rqdata,
            "verify_url": verify,
            "intent_id": found.get("intentId") or "",
            "client_secret": found.get("clientSecret") or "",
        }
    return None


def frame_host(frame_url):
    """The host the frame says the captcha is for."""
    if not frame_url:
        return ""
    tail = frame_url.split("#", 1)[-1]
    for part in tail.split("&"):
        if part.startswith("host="):
            return part[5:]
    return ""


# The code that watches what stripe's own captcha is asked for.  It is
# put on every frame before the page opens, so the key and the request
# data stripe uses are the ones the answer is asked for with.
WATCH_THE_CAPTCHA = """
(() => {
  const seen = [];
  const keys = {};      // the widget id -> the key it was put up with
  const tokens = {};    // the key -> the answer stripe's own captcha gave
  const rqdata = {};    // the key -> the request data stripe gave it
  const hook = (h) => {
    if (!h || h.__lcWatched) { return; }
    try {
      h.__lcWatched = true;
      const render = h.render.bind(h);
      const execute = h.execute.bind(h);
      h.render = function (sk, opts) {
        const id = render(sk, opts);
        const key = (opts && opts.sitekey) || sk || '';
        seen.push({how: 'render', sitekey: sk, id: id, opts: opts || {}});
        try { keys[id] = key; } catch (e) { }
        return id;
      };
      h.execute = function (a, opts) {
        seen.push({how: 'execute', what: a, opts: opts || {}});
        // The request data stripe itself gave the captcha is kept, and
        // so is the answer stripe's own captcha gives.  That answer is
        // about the request data that is on the page now, which is the
        // one stripe is waiting for.
        let key = '';
        try { key = keys[a] || ''; } catch (e) { }
        try {
          const rq = (opts && opts.rqdata)
            || ((opts && opts.enterprisePayload && opts.enterprisePayload.rqdata) || '');
          if (rq) { rqdata[key] = rq; }
        } catch (e) { }
        const p = execute(a, opts);
        try {
          if (p && p.then) {
            p.then((t) => {
              try {
                tokens[key] = (t && t.response) ? t.response : (t || '');
              } catch (e) { }
            }).catch(() => { });
          }
        } catch (e) { }
        return p;
      };
    } catch (e) { }
  };
  const timer = setInterval(() => {
    try { hook(window.hcaptcha); } catch (e) { }
    if (window.hcaptcha) { clearInterval(timer); }
  }, 50);
  setTimeout(() => clearInterval(timer), 20000);
  window.__lcSeen = seen;
  window.__lcTokens = tokens;
  window.__lcRqdata = rqdata;
})();
"""


async def page_captcha(client, session_id, pk):
    """The key and the request data the page itself came with.

    The captcha on the page is about the key that came with it, so the
    answer has to be asked for with that key and that request data.
    """
    from browser_identity import api_headers

    if not session_id or not pk:
        return None
    try:
        r = await client.get(
            f"https://api.stripe.com/v1/payment_pages/{session_id}?key={pk}",
            headers=api_headers(origin="https://checkout.stripe.com",
                                 referer="https://checkout.stripe.com/"),
            timeout=15,
        )
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    site_key = data.get("site_key") or ""
    rqdata = data.get("rqdata") or ""
    if not site_key:
        return None
    return {"site_key": site_key, "rqdata": rqdata}
