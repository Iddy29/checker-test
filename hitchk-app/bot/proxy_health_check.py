"""Check every proxy, the way the gates use them, and drop the dead ones."""
import concurrent.futures
import json
import os
import shutil
import time

import httpx

PATH = "/root/hitchecker/bot/user_proxies.json"
BACKUP = PATH + ".before_cleanup"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# the places a gate actually reaches
TARGETS = [
    ("paypal", "https://www.paypal.com/cgi-bin/webscr"),
    ("stripe", "https://api.stripe.com/v1/payment_methods"),
    ("shopify", "https://shopify.com/"),
    ("hcaptcha", "https://js.hcaptcha.com/1/api.js?render=explicit"),
]


def to_url(raw):
    """Every shape the proxy list uses."""
    raw = raw.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    parts = raw.split(":")
    if len(parts) >= 4:
        host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
        return "http://%s:%s@%s:%s" % (user, pw, host, port)
    if len(parts) == 2:
        return "http://%s:%s" % (parts[0], parts[1])
    return None


def check(raw):
    """A proxy is alive when the sites the gates use answer through it."""
    url = to_url(raw)
    if not url:
        return (raw, "bad format", "")
    out = []
    for name, target in TARGETS:
        try:
            r = httpx.get(target, proxy=url, timeout=18, follow_redirects=True,
                          headers={"User-Agent": UA})
            # a 401 or a 404 still means the address answered
            out.append("%s:%s" % (name, r.status_code))
        except Exception:
            out.append("%s:DEAD" % name)
    try:
        ri = httpx.get("https://api.ipify.org", proxy=url, timeout=15)
        ip = ri.text.strip()[:40]
    except Exception:
        ip = ""
    return (raw, " ".join(out), ip)


def main():
    data = json.load(open(PATH))
    all_raw = []
    for uid, v in data.items():
        if isinstance(v, dict):
            for raw in v.get("proxies", []):
                all_raw.append((uid, raw))

    print("the proxies to check:", len(all_raw))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda t: (t[0],) + check(t[1]), all_raw))

    good = {}
    for uid, raw, status, ip in results:
        dead = "DEAD" in status
        mark = "DEAD" if dead else "OK  "
        print("  %-4s %-36s %-50s %s" % (mark, raw[:36], status[:50], ip))
        if not dead:
            good.setdefault(uid, []).append(raw)

    print()
    print("the alive proxies:", sum(len(v) for v in good.values()), "of", len(results))

    # the dead ones go, the alive ones stay
    if not os.path.exists(BACKUP):
        shutil.copy(PATH, BACKUP)
        print("the backup is at", BACKUP)
    json.dump(good, open(PATH, "w"), indent=4)
    print("the list is written")

    # the authnet list too
    an = "/root/hitchecker/bot/authnet_proxies.json"
    if os.path.exists(an):
        try:
            lst = json.load(open(an))
        except Exception:
            lst = []
        keep = []
        for raw in lst:
            url = to_url(raw)
            if not url:
                continue
            try:
                httpx.get("https://api.ipify.org", proxy=url, timeout=15)
                keep.append(raw)
            except Exception:
                pass
        json.dump(keep, open(an, "w"), indent=1)
        print("the authnet list: %d -> %d" % (len(lst), len(keep)))


main()
