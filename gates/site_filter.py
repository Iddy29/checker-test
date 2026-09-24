"""
Smart Shopify Site Filter
- Tests sites from site.txt
- Filters out dead/captcha/cloudflare sites
- Returns only working sites for native checker
"""
import aiohttp
import asyncio
import random
import time
import os

SITE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "site.txt")
DEAD_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dead_sites.txt")
GOOD_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "good_sites.txt")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

DEAD_PATTERNS = [
    "captcha", "cloudflare", "access denied", "blocked",
    "forbidden", "unauthorized", "not found", "error 4",
    "maintenance", "unavailable", "temporarily down"
]

GOOD_PATTERNS = [
    "shopify", "checkout", "cart", "product",
    "add to cart", "buy now", "price"
]


def load_sites(filename):
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, 'r') as f:
            return [line.strip() for line in f if line.strip() and not line.startswith('#')]
    except:
        return []


def save_sites(filename, sites):
    try:
        with open(filename, 'w') as f:
            for site in sites:
                f.write(f"{site}\n")
    except:
        pass


async def test_site(session, site, timeout=8):
    clean_site = site.replace('https://', '').replace('http://', '').rstrip('/')
    
    test_urls = [
        f"https://{clean_site}",
        f"https://{clean_site}/products.json",
        f"https://{clean_site}/collections/all/products.json",
    ]
    
    for url in test_urls:
        try:
            headers = {
                'User-Agent': UA,
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
                text = await resp.text()
                text_lower = text.lower()
                
                if any(pattern in text_lower for pattern in DEAD_PATTERNS):
                    return False, "dead_pattern"
                
                if any(pattern in text_lower for pattern in GOOD_PATTERNS):
                    return True, "working"
                
                if resp.status == 200:
                    return True, "reachable"
                    
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
    
    return False, "unreachable"


async def filter_sites(parallel=10):
    all_sites = load_sites(SITE_FILE)
    dead_sites = load_sites(DEAD_FILE)
    
    test_sites = [s for s in all_sites if s not in dead_sites]
    
    if not test_sites:
        return load_sites(GOOD_FILE) or []
    
    working = []
    newly_dead = []
    
    connector = aiohttp.TCPConnector(limit=parallel, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for site in test_sites:
            tasks.append(test_site(session, site))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for site, result in zip(test_sites, results):
            if isinstance(result, Exception):
                newly_dead.append(site)
                continue
            
            is_working, reason = result
            if is_working:
                working.append(site)
            else:
                newly_dead.append(site)
    
    save_sites(GOOD_FILE, working)
    save_sites(DEAD_FILE, newly_dead)
    
    return working


async def get_working_sites(force_refresh=False):
    if force_refresh:
        return await filter_sites()
    
    good = load_sites(GOOD_FILE)
    if good and len(good) >= 3:
        return good
    
    return await filter_sites()


async def get_random_working_site():
    sites = await get_working_sites()
    if sites:
        return random.choice(sites)
    
    all_sites = load_sites(SITE_FILE)
    if all_sites:
        return random.choice(all_sites)
    
    return None
