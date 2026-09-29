import sys
import json
import asyncio
import os
import re
import aiohttp

sys.path.insert(0, os.path.dirname(__file__))

from gateways import run_gateway, parse_card_input, classify_response, get_flat_registry, get_user_proxy

# Use native Shopify checker (no external API)
from gates.shopify_native import shopify_native_check_rich, _format_response_v2


async def call_shopify_api(cc, mm, yy, cvv, site=None, proxy=None, timeout=90):
    """Shopify Native Checker - Real bank responses."""
    from datetime import datetime
    import random
    import time as _time
    start_time = _time.time()
    
    # Check card expiry first
    try:
        year_full = f"20{yy}" if len(yy) == 2 else yy
        exp_date = datetime(int(year_full), int(mm), 1)
        now = datetime.now()
        if exp_date.year < now.year or (exp_date.year == now.year and exp_date.month < now.month):
            return {
                "status": "declined",
                "response": "Declined - Card Expired",
                "gateway": "Shopify Payments",
                "amount": None,
                "site": None,
                "elapsed": 0,
                "extra": None,
            }
    except:
        pass
    
    try:
        result = await shopify_native_check_rich(cc, mm, yy, cvv, site=site, proxy=proxy)
        status = result.get("status", "error")
        response_text = result.get("response", "")
        
        # Skip site errors
        site_error_keywords = [
            "No products", "Site error", "requires login", "No shipping",
            "No session token", "Captcha", "Checkpoint", "OUT_OF_STOCK",
            "MERCHANDISE_OUT_OF_STOCK", "Currency not supported",
            "GATEWAY_UNAVAILABLE", "DEVELOPMENT_STORE", "INVALID_VARIABLE",
            "ARTIFACT_DISSATISFACTION", "Gateway Error",
            "Cart error", "Checkout error", "Vault error", "Payment error",
            "All sites failed", "Timeout", "Negotiate error",
            "WAITING_PENDING_TERMS", "TAX_NEW_TAX_MUST_BE_ACCEPTED",
        ]
        
        if any(kw in response_text for kw in site_error_keywords):
            return {
                "status": "dead_site",
                "response": response_text,
                "gateway": "Shopify Payments",
                "amount": None,
                "site": result.get("site"),
                "elapsed": round(_time.time() - start_time, 2),
                "extra": None,
            }
        
        return {
            "status": status,
            "response": _format_response_v2(result),
            "gateway": result.get("gateway", "Shopify Payments"),
            "amount": result.get("amount"),
            "site": result.get("site"),
            "elapsed": round(_time.time() - start_time, 2),
            "extra": result.get("extra"),
        }
    except Exception as e:
        return {
            "status": "error",
            "response": f"Error: {str(e)[:100]}",
            "gateway": "Shopify Payments",
            "amount": None,
            "site": site,
            "elapsed": 0,
            "extra": None,
        }

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

    # ── Shopify: use hosted API ────────────────────────────────────────────
    if alias == "shp" and user_id:
        try:
            _shp_proxy = None  # No proxy for Shopify - proxy blocks checkout

            # Load user sites + admin sites
            sites_file = os.path.join(os.path.dirname(__file__), "user_sites.json")
            custom_sites = []
            if os.path.exists(sites_file):
                with open(sites_file, "r") as f:
                    all_sites = json.load(f)
                custom_sites = all_sites.get(str(user_id), [])

            admin_sites_file = os.path.join(os.path.dirname(__file__), "admin_sites.json")
            if os.path.exists(admin_sites_file):
                with open(admin_sites_file, "r") as f:
                    admin_sites = json.load(f)
                if isinstance(admin_sites, list):
                    custom_sites = list(set(custom_sites + admin_sites))

            if custom_sites:
                import random as _rand
                _rand.shuffle(custom_sites)

                for site in custom_sites[:10]:
                    try:
                        result = await asyncio.wait_for(
                            call_shopify_api(cc, mm, yy, cvv, site=site, proxy=_shp_proxy, timeout=90),
                            timeout=95
                        )
                        if result.get("status") not in ("dead_site", "error"):
                            result_str = result.get("response", "")
                            classification = classify_response(result_str)
                            return {
                                "status": classification.lower(),
                                "response": clean_response(result_str),
                                "gateway": result.get("gateway", "Shopify"),
                                "card": f"{cc}|{mm}|{yy}|{cvv}",
                                "site": result.get("site", ""),
                                "amount": result.get("amount"),
                                "confidence": result.get("confidence"),
                                "explanation": result.get("explanation", ""),
                                "card_type": result.get("card_type", ""),
                                "card_bin": result.get("card_bin", ""),
                                "card_last4": result.get("card_last4", ""),
                            }
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        continue

            # No custom sites (or all failed): let the API pick from its own list
            try:
                result = await asyncio.wait_for(
                    call_shopify_api(cc, mm, yy, cvv, site=None, proxy=_shp_proxy, timeout=90),
                    timeout=95
                )
                result_str = result.get("response", "")
                classification = classify_response(result_str)
                return {
                    "status": classification.lower(),
                    "response": clean_response(result_str),
                    "gateway": result.get("gateway", "Shopify"),
                    "card": f"{cc}|{mm}|{yy}|{cvv}",
                    "site": result.get("site", ""),
                    "amount": result.get("amount"),
                    "confidence": result.get("confidence"),
                    "explanation": result.get("explanation", ""),
                    "card_type": result.get("card_type", ""),
                    "card_bin": result.get("card_bin", ""),
                    "card_last4": result.get("card_last4", ""),
                }
            except Exception as e:
                return {"status": "error", "response": f"Shopify API error: {str(e)[:150]}", "card": f"{cc}|{mm}|{yy}|{cvv}"}
        except Exception:
            pass

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
        return {
            "status": classification.lower(),
            "response": clean_response(result),
            "gateway": gate_info["name"],
            "card": f"{cc}|{mm}|{yy}|{cvv}"
        }
    except asyncio.TimeoutError:
        return {"status": "error", "response": f"Gateway timeout ({timeout_secs}s)"}
    except Exception as e:
        return {"status": "error", "response": f"Error: {str(e)[:200]}"}

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
