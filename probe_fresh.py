import asyncio, sys
sys.path.insert(0, "/root/hitchecker/hitchk-app/bot")
from gates import cheap_fetcher, shopify_native_gate as gate

async def main():
    start = 0
    while start < 3000:
        out = await cheap_fetcher.probe_the_pool(words=None)
        print("the round from", start, ":", out, flush=True)
        start += 260

asyncio.run(main())
