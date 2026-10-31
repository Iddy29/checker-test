"""Bridge for the Authorize.net (/an) gateway API.

Usage:
    python3 web_authnet_check.py <cc> <mm> <yy> <cvv> [user_id] [is_admin]

Prints a single JSON object on stdout.  Any extra noise goes to stderr so
the caller can pick the last line that starts with '{'.
"""

import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from gates.authnet import authnet_check
from gateways import classify_response, get_user_proxy


def _parse_card(card):
    parts = [p.strip() for p in str(card).split("|")]
    if len(parts) < 4:
        return None
    cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    if not cc.isdigit() or not mm.isdigit() or not yy.isdigit() or not cvv.isdigit():
        return None
    if len(cc) < 13 or len(cc) > 19:
        return None
    if len(yy) == 4:
        yy = yy[2:]
    return cc, mm.zfill(2), yy.zfill(2), cvv


async def main():
    if len(sys.argv) < 5:
        print(json.dumps({
            "status": "error",
            "response": "Usage: web_authnet_check.py <cc> <mm> <yy> <cvv> [user_id] [is_admin]",
        }))
        return

    raw = "|".join(sys.argv[1:5])
    parsed = _parse_card(raw)
    if not parsed:
        print(json.dumps({
            "status": "error",
            "response": "Invalid card format. Use: CC|MM|YY|CVV",
        }))
        return

    cc, mm, yy, cvv = parsed
    user_id = sys.argv[5] if len(sys.argv) > 5 else None
    is_admin = (sys.argv[6].lower() == "true") if len(sys.argv) > 6 else False

    proxy = None
    if user_id:
        try:
            proxy = get_user_proxy(str(user_id))
        except Exception:
            proxy = None

    try:
        result = await authnet_check(cc, mm, yy, cvv, proxy=proxy)
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "response": f"Gateway error: {type(e).__name__}: {str(e)[:150]}",
            "gateway": "Authorize.net Auth",
            "card": f"{cc}|{mm}|{yy}|{cvv}",
        }))
        return

    result = str(result or "No response from gateway")
    status = classify_response(result).lower()
    response = result
    # Strip the trailing [12.3s] timing suffix.
    if response.endswith("]") and "[" in response:
        response = response.rsplit("[", 1)[0].strip()

    print(json.dumps({
        "status": status,
        "response": response,
        "gateway": "Authorize.net Auth",
        "card": f"{cc}|{mm}|{yy}|{cvv}",
        "amount": "$0.01",
        "site": "bethlehemdental.dentist",
    }))


if __name__ == "__main__":
    asyncio.run(main())
