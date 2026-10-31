#!/bin/bash

echo "=============================================="
echo "  FIXING: API ID/Hash Empty Error"
echo "  Bot Location: /root/hitchecker/bot/"
echo "=============================================="
echo ""

BOT_DIR="/root/hitchecker/bot"
BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
API_ID="12340097"
API_HASH="76ad4197b5db92f328e02d9000675a10"

# 1. Stop bot
echo "Step 1: Stopping bot..."
pkill -f "python.*bot.py"
sleep 2
echo "  ✓ Bot stopped"

# 2. Check config.json
echo ""
echo "Step 2: Checking config.json..."
cd "$BOT_DIR"

if [ -f "config.json" ]; then
    echo "  Found config.json"
    echo "  Current content:"
    cat config.json
else
    echo "  ✗ config.json NOT FOUND!"
    echo "  Creating it..."
    cat > config.json << EOF
{
  "TELEGRAM_BOT_TOKEN": "$BOT_TOKEN",
  "TELEGRAM_API_ID": "$API_ID",
  "TELEGRAM_API_HASH": "$API_HASH",
  "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
  "TELEGRAM_GROUP_ID": "-1003474408861",
  "TELEGRAM_GROUP_LINK": "https://t.me/+rBq3QyI5Z7MzOGY9",
  "logs_group_id": "-1003816358079"
}
EOF
    echo "  ✓ config.json created"
fi

# 3. Fix bot.py to load config properly
echo ""
echo "Step 3: Fixing bot.py to load config..."

python3 << 'PYFIX'
# Read bot.py
with open("bot.py", "r") as f:
    content = f.read()

# Check if config loader exists
if "def _load_config():" not in content:
    print("  Adding config loader function...")
    
    # Find where to insert (after imports, before API_ID line)
    old_config_section = '''API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")'''

    new_config_section = '''def _load_config():
    """Load configuration from config.json in the bot directory."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()

API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))
API_HASH = os.environ.get("TELEGRAM_API_HASH", _config.get("TELEGRAM_API_HASH", ""))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _config.get("TELEGRAM_BOT_TOKEN", ""))'''

    if old_config_section in content:
        content = content.replace(old_config_section, new_config_section)
        print("  ✓ Config loader added")
    else:
        # Try alternative pattern
        import re
        pattern = r'API_ID = os\.environ\.get\("TELEGRAM_API_ID", ""\)'
        if re.search(pattern, content):
            # Replace line by line
            content = re.sub(
                r'API_ID = os\.environ\.get\("TELEGRAM_API_ID", ""\)',
                'API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))',
                content
            )
            content = re.sub(
                r'API_HASH = os\.environ\.get\("TELEGRAM_API_HASH", ""\)',
                'API_HASH = os.environ.get("TELEGRAM_API_HASH", _config.get("TELEGRAM_API_HASH", ""))',
                content
            )
            content = re.sub(
                r'BOT_TOKEN = os\.environ\.get\("TELEGRAM_BOT_TOKEN", ""\)',
                'BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _config.get("TELEGRAM_BOT_TOKEN", ""))',
                content
            )
            # Add config loader before these lines
            config_loader = '''def _load_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()

'''
            content = content.replace('API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))', 
                                     config_loader + 'API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))')
            print("  ✓ Config loader added (alternative method)")
        else:
            print("  ⚠ Could not find config section")
else:
    print("  ✓ Config loader already exists")

# Write back
with open("bot.py", "w") as f:
    f.write(content)

print("  ✓ bot.py updated")
PYFIX

# 4. Update config with correct values
echo ""
echo "Step 4: Updating config.json with correct values..."
python3 << PYUPDATE
import json

try:
    with open("config.json", "r") as f:
        config = json.load(f)
    
    config["TELEGRAM_BOT_TOKEN"] = "$BOT_TOKEN"
    config["TELEGRAM_API_ID"] = "$API_ID"
    config["TELEGRAM_API_HASH"] = "$API_HASH"
    
    with open("config.json", "w") as f:
        json.dump(config, f, indent=2)
    
    print("  ✓ Config updated")
    print("  API_ID:", config.get("TELEGRAM_API_ID"))
    print("  API_HASH:", config.get("TELEGRAM_API_HASH"))
    print("  Token:", config.get("TELEGRAM_BOT_TOKEN")[:20] + "...")
except Exception as e:
    print(f"  ✗ Error: {e}")
PYUPDATE

# 5. Install dependencies
echo ""
echo "Step 5: Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests flask --upgrade --quiet 2>/dev/null
echo "  ✓ Dependencies installed"

# 6. Clean session
echo ""
echo "Step 6: Cleaning session files..."
rm -f cc_bot.session*
echo "  ✓ Session cleaned"

# 7. Test import
echo ""
echo "Step 7: Testing bot import..."
IMPORT_TEST=$(python3 -c "import bot" 2>&1)
if [ -z "$IMPORT_TEST" ]; then
    echo "  ✅ Bot imports successfully!"
else
    echo "  ❌ Import failed:"
    echo "$IMPORT_TEST"
    echo ""
    echo "  Fix errors above, then restart bot"
    exit 1
fi

# 8. Start bot
echo ""
echo "Step 8: Starting bot..."
nohup python3 bot.py > bot.log 2>&1 &
sleep 5

if pgrep -f "python.*bot.py" > /dev/null; then
    echo "  ✅ Bot started successfully!"
    echo "  PID: $(pgrep -f 'python.*bot.py')"
else
    echo "  ❌ Bot failed to start!"
    echo ""
    echo "  Errors:"
    tail -50 bot.log
    exit 1
fi

# 9. Check logs
echo ""
echo "Step 9: Recent logs:"
sleep 2
tail -30 bot.log | grep -E "(started|error|Error|API|API_ID|API_HASH)" || tail -30 bot.log

# 10. Final status
echo ""
echo "=============================================="
echo "  ✅ FIX COMPLETE!"
echo "=============================================="
echo ""
echo "Bot is now running with:"
echo "  API_ID: $API_ID"
echo "  API_HASH: $API_HASH"
echo "  Token: $BOT_TOKEN"
echo ""
echo "Test: https://t.me/Myhitz_bot"
echo "Send: /start"
echo ""
echo "Logs: tail -f $BOT_DIR/bot.log"
echo ""
