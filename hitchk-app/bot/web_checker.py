import sys
import json
import asyncio
import os
import re

sys.path.insert(0, os.path.dirname(__file__))

from gateways import (run_gateway, parse_card_input, classify_response,
                      get_flat_registry, st_verdict_token)
from gates.shopify_api import call_shopify_api as _shopify_api_call


def clean_response(raw):
    text = str(raw)
    text = re.sub(r'\s*\[\d+\.?\d*s\]\s*$', '', text)
    text = re.sub(r'\s*\|\s*(?:VISA|MASTERCARD|AMEX|DISCOVER|JCB|DINERS|MAESTRO|UNIONPAY|CARD)(?:\s+(?:CREDIT|DEBIT|PREPAID|CHARGE))?\s*\|.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\|\s*\d{4,6}\s*$', '', text)
    text = re.sub(r'^(?:Declined|Approved|Error|Unknown)\s*-\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:Declined|Approved|Error|Unknown)\s*-\s*', '', text, flags=re.IGNORECASE)
    return text.strip() or str(raw).strip()

async def check_card(alias, card_str, user_id=None, is_admin=False):
    parsed = parse_card_input(card_str)
    if not parsed:
        return {"status": "error", "response": "Invalid card format. Use: CC|MM|YY|CVV"}

    cc, mm, yy, cvv = parsed

    flat = get_flat_registry()
    gate_info = flat.get(alias)
    if not gate_info:
        return {"status": "error", "response": f"Unknown gateway: {alias}"}

    timeout_secs = 120 if alias in ("auto", "autoskool", "shp") else 60

    # NOTE: the old hosted-Shopify branch (one call per user site, then a
    # second call with site=None) was removed.  Shopify now goes through the
    # local validator API, so a single check runs exactly once.

    if alias == "shp":
        try:
            result = await asyncio.wait_for(
                _shopify_api_call(cc, mm, yy, cvv, site=None, timeout=timeout_secs),
                timeout=timeout_secs + 5,
            )
            result_str = result.get("response", "Unknown")
            classification = classify_response(result_str)
            if result.get("status") == "error":
                classification = "error"
            elif result.get("status") == "approved":
                classification = "approved"
            elif result.get("status") == "declined":
                classification = "declined"
            return {
                "status": classification.lower(),
                "response": clean_response(result_str),
                "gateway": result.get("gateway", "Shopify"),
                "card": f"{cc}|{mm}|{yy}|{cvv}",
                "site": result.get("site", ""),
                "amount": result.get("amount"),
                "elapsed": result.get("elapsed", 0),
            }
        except asyncio.TimeoutError:
            return {"status": "error", "response": f"Gateway timeout ({timeout_secs}s)"}
        except Exception as e:
            return {"status": "error", "response": f"Error: {type(e).__name__}: {str(e)[:200]}"}

    try:
        result = await asyncio.wait_for(
            run_gateway(alias, cc, mm, yy, cvv, user_id=user_id, use_semaphore=False, is_admin=is_admin),
            timeout=timeout_secs
        )
        result_str = str(result)
        if result_str == "NO_SKOOL_ACCOUNT":
            return {
                "status": "error",
                "response": "NO_SKOOL_ACCOUNT",
                "gateway": gate_info["name"],
                "card": f"{cc}|{mm}|{yy}|{cvv}"
            }
        classification = classify_response(result_str)
        response_text = clean_response(result)
        if alias == "st":
            # the /st verdict is one word: approved, approved with 3ds
            # or card_decline -- never the reason code
            token = st_verdict_token(classification)
            if token:
                low = (response_text or "").lower()
                if token == "approved" and ("3ds" in low or "3d secure" in low
                                            or "requires_action" in low):
                    response_text = "approved with 3ds"
                else:
                    response_text = token
        return {
            "status": classification.lower(),
            "response": response_text,
            "gateway": gate_info["name"],
            "card": f"{cc}|{mm}|{yy}|{cvv}"
        }
    except asyncio.TimeoutError:
        return {"status": "error", "response": f"Gateway timeout ({timeout_secs}s)"}
    except Exception as e:
        return {"status": "error", "response": f"Error: {type(e).__name__}: {str(e)[:200]}"}

async def main():
    if len(sys.argv) < 3:
        print(json.dumps({"status": "error", "response": "Usage: web_checker.py <gateway> <card> [user_id]"}))
        return

    alias = sys.argv[1]
    card_str = sys.argv[2]
    user_id = sys.argv[3] if len(sys.argv) > 3 else None
    is_admin = sys.argv[4].lower() == "true" if len(sys.argv) > 4 else False

    result = await check_card(alias, card_str, user_id=user_id, is_admin=is_admin)
    print(json.dumps(result))

if __name__ == "__main__":
    asyncio.run(main())
