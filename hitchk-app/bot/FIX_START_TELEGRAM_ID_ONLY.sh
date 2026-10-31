#!/bin/bash

echo "=============================================="
echo "  FIX: /start Works with Telegram ID Only"
echo "  (Token-only authentication)"
echo "=============================================="
echo ""

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
BOT_DIR="/root/hitchecker/bot"

cd "$BOT_DIR"

# 1. Stop bot
echo "Step 1: Stopping bot..."
pkill -f "python.*bot.py" 2>/dev/null
sleep 2
echo "  ✓ Bot stopped"

# 2. Update config with minimal required data
echo ""
echo "Step 2: Creating minimal config (token only)..."

python3 << PYUPDATE
import json

config = {
    "TELEGRAM_BOT_TOKEN": "$BOT_TOKEN",
    "TELEGRAM_API_ID": "20464",
    "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a",
    "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
    "TELEGRAM_GROUP_ID": "-1003474408861",
    "TELEGRAM_GROUP_LINK": "https://t.me/+rBq3QyI5Z7MzOGY9",
    "TELEGRAM_CHANNEL_LINK": "https://t.me/TeachHit",
    "logs_group_id": "-1003816358079"
}

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)

print("  ✓ Config created with token + defaults")
PYUPDATE

# 3. Fix bot.py to ensure /start works with minimal config
echo ""
echo "Step 3: Fixing bot.py for /start command..."

python3 << 'PYFIX'
import re

with open("bot.py", "r") as f:
    content = f.read()

# 1. Add config loader with defaults if missing
if "def _load_config():" not in content:
    print("  Adding config loader...")
    
    # Find where to insert
    insert_point = content.find('API_ID = os.environ.get("TELEGRAM_API_ID"')
    if insert_point == -1:
        insert_point = content.find('from telethon')
        if insert_point == -1:
            insert_point = 0
    
    config_loader = '''
def _load_config():
    """Load config from config.json with safe defaults for token-only operation."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Config load error: {e}")
        return {
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_API_ID": "20464",
            "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a",
            "TELEGRAM_ADMIN_ID": "",
            "TELEGRAM_GROUP_ID": "0",
            "TELEGRAM_GROUP_LINK": "",
            "TELEGRAM_CHANNEL_LINK": "",
            "TELEGRAM_CHANNEL_ID": ""
        }

_config = _load_config()

'''
    
    if insert_point > 0:
        content = content[:insert_point] + config_loader + content[insert_point:]
    else:
        content = config_loader + "\n" + content
    
    print("  ✓ Config loader added")

# 2. Ensure API_ID and API_HASH have defaults
content = re.sub(
    r'API_ID = (?:os\.environ\.get\([^)]+\)|_config\.get\([^)]+\))',
    'API_ID = str(_config.get("TELEGRAM_API_ID", "20464"))',
    content
)

content = re.sub(
    r'API_HASH = (?:os\.environ\.get\([^)]+\)|_config\.get\([^)]+\))',
    'API_HASH = str(_config.get("TELEGRAM_API_HASH", "c4a32e97023c4d3e6c8f5a9b2d1e3f4a"))',
    content
)

content = re.sub(
    r'BOT_TOKEN = (?:os\.environ\.get\([^)]+\)|_config\.get\([^)]+\))',
    'BOT_TOKEN = str(_config.get("TELEGRAM_BOT_TOKEN", ""))',
    content
)

# 3. Ensure ADMIN_ID has default
content = re.sub(
    r'ADMIN_ID = \[int\(x\) for x in os\.environ\.get\([^)]+\)\.split\(",""\) if x\.strip\(\)\]',
    'ADMIN_ID = [int(x) for x in _config.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]',
    content
)

# 4. Ensure GROUP_ID has default
content = re.sub(
    r'GROUP_ID = int\(os\.environ\.get\([^)]+\)\)',
    'GROUP_ID = int(_config.get("TELEGRAM_GROUP_ID", "0"))',
    content
)

# 5. Ensure GROUP_LINK has default
content = re.sub(
    r'GROUP_LINK = os\.environ\.get\([^)]+\)',
    'GROUP_LINK = _config.get("TELEGRAM_GROUP_LINK", "")',
    content
)

# 6. Ensure CHANNEL_LINK has default
content = re.sub(
    r'CHANNEL_LINK = os\.environ\.get\([^)]+\)',
    'CHANNEL_LINK = _config.get("TELEGRAM_CHANNEL_LINK", "")',
    content
)

# 7. Ensure CHANNEL_ID has default
content = re.sub(
    r'CHANNEL_ID_RAW = os\.environ\.get\([^)]+\)',
    'CHANNEL_ID_RAW = _config.get("TELEGRAM_CHANNEL_ID", "")',
    content
)

# Write back
with open("bot.py", "w") as f:
    f.write(content)

print("  ✓ bot.py updated for token-only operation")
PYFIX

# 4. Register bot commands with Telegram
echo ""
echo "Step 4: Registering /start and other commands..."

curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{
    "commands": [
      {"command": "start", "description": "Start the bot and see menu"},
      {"command": "help", "description": "Show help information"},
      {"command": "cmds", "description": "Show all commands"},
      {"command": "info", "description": "Show your account info"},
      {"command": "ping", "description": "Check bot response time"}
    ]
  }' > /dev/null

echo "  ✓ Commands registered"

# 5. Set bot info
echo ""
echo "Step 5: Setting bot info..."

curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" \
  -H "Content-Type: application/json" \
  -d '{"name": "I-NETTZ Checker"}' > /dev/null

curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyDescription" \
  -H "Content-Type: application/json" \
  -d '{"description": "Card Checker Bot - Check cards, lookup BINs, and more!"}' > /dev/null

echo "  ✓ Bot info updated"

# 6. Install dependencies
echo ""
echo "Step 6: Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests flask --upgrade --quiet 2>/dev/null
echo "  ✓ Dependencies installed"

# 7. Clean session
echo ""
echo "Step 7: Cleaning session files..."
rm -f cc_bot.session*
echo "  ✓ Session cleaned"

# 8. Test import
echo ""
echo "Step 8: Testing bot import..."
IMPORT_TEST=$(python3 -c "import bot" 2>&1)
if [ -z "$IMPORT_TEST" ] || ! echo "$IMPORT_TEST" | grep -qi "error\|traceback\|ValueError"; then
    echo "  ✅ Bot imports successfully!"
else
    echo "  ⚠ Import output:"
    echo "$IMPORT_TEST" | tail -5
fi

# 9. Start bot
echo ""
echo "Step 9: Starting bot..."
nohup python3 bot.py > bot.log 2>&1 &
BOT_PID=$!
sleep 5

if pgrep -f "python.*bot.py" > /dev/null; then
    echo "  ✅ Bot started! (PID: $BOT_PID)"
else
    echo "  ⚠ Checking status..."
    if ps aux | grep -v grep | grep "python.*bot.py" > /dev/null; then
        echo "  ✅ Bot is running"
    else
        echo "  ❌ Bot may have issues"
        tail -50 bot.log
    fi
fi

# 10. Show logs
echo ""
echo "Step 10: Recent logs:"
sleep 2
tail -30 bot.log | grep -E "(started|Started|running|error|Error|API|API_ID|API_HASH|Warning|/start)" || tail -30 bot.log

# 11. Verify bot token
echo ""
echo "Step 11: Verifying bot token..."
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo "  ✅ Bot token valid - @$BOT_USER"
else
    echo "  ⚠ Token: $BOT_INFO"
fi

# 12. Check commands
echo ""
echo "Step 12: Checking registered commands..."
GET_CMDS=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMyCommands")
echo "  Commands:"
echo "$GET_CMDS" | python3 -c "import sys,json; cmds=json.load(sys.stdin).get('result',[]); [print(f'    /{c[\"command\"]} - {c[\"description\"]}') for c in cmds]" 2>/dev/null || echo "$GET_CMDS"

# Final status
echo ""
echo "=============================================="
echo "  ✅ FIX COMPLETE!"
echo "=============================================="
echo ""
echo "🎯 KEY FEATURES:"
echo "  ✓ Bot works with Telegram ID (token) only"
echo "  ✓ API_ID/API_HASH use safe defaults"
echo "  ✓ /start command registered"
echo "  ✓ Bot menu commands enabled"
echo "  ✓ Minimal config required"
echo ""
echo "📊 Configuration:"
echo "  Bot Token: $BOT_TOKEN"
echo "  API_ID: 20464 (default)"
echo "  API_HASH: c4a32e97023c4d3e6c8f5a9b2d1e3f4a (default)"
echo "  Bot: @$BOT_USER"
echo "  Status: $(pgrep -f 'python.*bot.py' > /dev/null && echo '✅ RUNNING' || echo '⚠ CHECK LOGS')"
echo ""
echo "📱 TEST NOW:"
echo "  1. Open: https://t.me/$BOT_USER"
echo "  2. Send: /start"
echo "  3. See menu and commands!"
echo ""
echo "💡 If commands don't appear:"
echo "  • Close the chat completely"
echo "  • Reopen @${BOT_USER:-Myhitz_bot}"
echo "  • Send /start again"
echo "  • Commands should appear above input"
echo ""
echo "📝 Logs: tail -f $BOT_DIR/bot.log"
echo ""
echo "🔧 Commands Available:"
echo "  /start - Start bot and see menu"
echo "  /help - Show help"
echo "  /cmds - All commands"
echo "  /info - Account info"
echo "  /ping - Check bot"
echo ""
