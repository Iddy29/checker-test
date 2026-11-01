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
        const opts = { async: true };
        if (rqdata) {
            opts.enterprisePayload = { rqdata: rqdata };
        }
        // The captcha has to be put up before it can be asked for an
        // answer.  It is put up on the host it is told, which is the
        // host of the frame it is in.
        let id = null;
        try {
            const box = document.createElement('div');
            box.style.position = 'fixed';
            box.style.left = '0px';
            box.style.top = '0px';
            box.style.width = '1px';
            box.style.height = '1px';
            box.style.opacity = '0.01';
            box.style.zIndex = '2147483647';
            document.body.appendChild(box);
            const renderOpts = {
                sitekey: sitekey,
                callback: keep,
                'error-callback': (e) => { window.__lcError = String(e); }
            };
            if (rqdata) {
                renderOpts.enterprisePayload = { rqdata: rqdata };
            }
            id = window.hcaptcha.render(box, renderOpts);
        } catch (e) {
            window.__lcError = 'the render failed: ' + e;
            return 'the render failed: ' + e;
        }
        window.__lcId = id;
        const p = window.hcaptcha.execute(id, opts);
        if (!p || !p.then) { return 'execute gave nothing'; }
        p.then(keep).catch((e) => { window.__lcError = String(e); });
        return 'asked';
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
  const hook = (h) => {
    if (!h || h.__lcWatched) { return; }
    try {
      h.__lcWatched = true;
      const render = h.render.bind(h);
      const execute = h.execute.bind(h);
      h.render = function (sk, opts) {
        const id = render(sk, opts);
        seen.push({how: 'render', sitekey: sk, id: id, opts: opts || {}});
        return id;
      };
      h.execute = function (a, opts) {
        seen.push({how: 'execute', what: a, opts: opts || {}});
        return execute(a, opts);
      };
    } catch (e) { }
  };
  const timer = setInterval(() => {
    try { hook(window.hcaptcha); } catch (e) { }
    if (window.hcaptcha) { clearInterval(timer); }
  }, 50);
  setTimeout(() => clearInterval(timer), 20000);
  window.__lcSeen = seen;
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
