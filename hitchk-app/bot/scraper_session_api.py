import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

def _settings():
    """The bot's settings, from the file the service loads.

    The bot reads bot.env, so the session setup has to read the same
    file and not the old config.json.
    """
    out = {}
    env_path = os.path.join(os.path.dirname(__file__), "bot.env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except Exception:
        pass
    try:
        with open(os.path.join(os.path.dirname(__file__), "config.json"), "r") as f:
            config = json.load(f)
        for key, value in config.items():
            out.setdefault(key, value)
    except Exception:
        pass
    return out


async def send_code(phone):
    config = _settings()

    api_id = int(config.get("TELEGRAM_API_ID", "0") or 0)
    api_hash = config.get("TELEGRAM_API_HASH", "") or ""
    session_path = os.path.join(os.path.dirname(__file__), "scraper_user")

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(phone)
        print(json.dumps({
            "success": True,
            "phoneCodeHash": result.phone_code_hash,
            "message": "Code sent to your Telegram app"
        }))
    except Exception as e:
        print(json.dumps({"error": str(e)[:300]}))
    finally:
        await client.disconnect()


async def verify_code(phone, code, phone_code_hash, password=None):
    config = _settings()

    api_id = int(config.get("TELEGRAM_API_ID", "0") or 0)
    api_hash = config.get("TELEGRAM_API_HASH", "") or ""
    session_path = os.path.join(os.path.dirname(__file__), "scraper_user")

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if password:
                await client.sign_in(password=password)
            else:
                print(json.dumps({
                    "error": "2FA password required",
                    "needs_password": True
                }))
                return

        me = await client.get_me()
        print(json.dumps({
            "success": True,
            "user": {
                "id": me.id,
                "firstName": me.first_name or "",
                "lastName": me.last_name or "",
                "username": me.username or ""
            },
            "message": f"Authenticated as {me.first_name} {me.last_name or ''}"
        }))
    except Exception as e:
        print(json.dumps({"error": str(e)[:300]}))
    finally:
        await client.disconnect()


async def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: scraper_session_api.py <send_code|verify> <phone> [code] [hash] [password]"}))
        return

    action = sys.argv[1]
    phone = sys.argv[2]

    if action == "send_code":
        await send_code(phone)
    elif action == "verify":
        if len(sys.argv) < 5:
            print(json.dumps({"error": "Missing code or phone_code_hash"}))
            return
        code = sys.argv[3]
        phone_code_hash = sys.argv[4]
        password = sys.argv[5] if len(sys.argv) > 5 else None
        await verify_code(phone, code, phone_code_hash, password)
    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))

if __name__ == "__main__":
    asyncio.run(main())
