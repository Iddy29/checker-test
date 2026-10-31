#!/bin/bash
# Quick Fix Script - Run this on VPS to fix the Telegram bot

echo "=== Telegram Bot Quick Fix ==="
echo ""

# Find bot directory
cd /opt/telegram-bot 2>/dev/null || cd ~/telegram-bot 2>/dev/null || cd ~ 2>/dev/null || { echo "ERROR: Cannot find bot dir"; exit 1; }

echo "Working directory: $(pwd)"
echo ""

# Backup bot.py
echo "1. Backing up bot.py..."
cp bot.py bot.py.backup.$(date +%s) 2>/dev/null && echo "   ✓ Backup created"

# Install dependencies
echo "2. Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests --quiet --upgrade && echo "   ✓ Dependencies installed"

# Fix configuration loading
echo "3. Fixing configuration loading..."
python3 << 'PYFIX'
try:
    with open("bot.py", "r") as f:
        content = f.read()
    
    # Add config loader function after imports
    if "def _load_config():" not in content:
        # Find where to insert (after the imports section)
        import_section_end = content.find('\n\nAPI_ID = os.environ.get')
        if import_section_end > 0:
            config_func = '''

def _load_config():
    """Load configuration from config.json in the bot directory."""
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()
'''
            content = content.replace('API_ID = os.environ.get("TELEGRAM_API_ID", "")', 
                                     config_func + 'API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))')
            content = content.replace('API_HASH = os.environ.get("TELEGRAM_API_HASH", "")', 
                                     'API_HASH = os.environ.get("TELEGRAM_API_HASH", _config.get("TELEGRAM_API_HASH", ""))')
            content = content.replace('BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")', 
                                     'BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _config.get("TELEGRAM_BOT_TOKEN", ""))')
            content = content.replace('ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]', 
                                     'ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", _config.get("TELEGRAM_ADMIN_ID", "")).split(",") if x.strip()]')
            content = content.replace('GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", "0"))', 
                                     'GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", _config.get("TELEGRAM_GROUP_ID", "0")))')
            content = content.replace('GROUP_LINK = os.environ.get("TELEGRAM_GROUP_LINK", "")', 
                                     'GROUP_LINK = os.environ.get("TELEGRAM_GROUP_LINK", _config.get("TELEGRAM_GROUP_LINK", ""))')
            content = content.replace('CHANNEL_LINK = os.environ.get("TELEGRAM_CHANNEL_LINK", "")', 
                                     'CHANNEL_LINK = os.environ.get("TELEGRAM_CHANNEL_LINK", _config.get("TELEGRAM_CHANNEL_LINK", ""))')
            content = content.replace('CHANNEL_ID_RAW = os.environ.get("TELEGRAM_CHANNEL_ID", "")', 
                                     'CHANNEL_ID_RAW = os.environ.get("TELEGRAM_CHANNEL_ID", _config.get("TELEGRAM_CHANNEL_ID", ""))')
            
            with open("bot.py", "w") as f:
                f.write(content)
            print("   ✓ Configuration loading fixed")
        else:
            print("   ⚠ Could not find configuration section")
    else:
        print("   ✓ Configuration already fixed")
except Exception as e:
    print(f"   ✗ Error: {e}")
PYFIX

# Test import
echo "4. Testing bot import..."
if python3 -c "import bot" 2>&1 | grep -q "error\|Error\|ERROR"; then
    echo "   ✗ Import failed, checking error..."
    python3 -c "import bot" 2>&1 | tail -5
else
    echo "   ✓ Bot imports successfully"
fi

# Restart bot
echo "5. Restarting bot..."
if systemctl is-active telegram-bot >/dev/null 2>&1; then
    systemctl restart telegram-bot
    sleep 2
    if systemctl is-active telegram-bot >/dev/null 2>&1; then
        echo "   ✓ Bot restarted (systemd service)"
    else
        echo "   ⚠ Service restart failed"
    fi
elif pgrep -f "python.*bot.py" >/dev/null; then
    echo "   ✓ Bot is running (manual process)"
    echo "   Restarting manually..."
    pkill -f "python.*bot.py"
    sleep 2
    nohup python3 bot.py > bot.log 2>&1 &
    sleep 2
    if pgrep -f "python.*bot.py" >/dev/null; then
        echo "   ✓ Bot restarted"
    else
        echo "   ✗ Failed to restart bot"
    fi
else
    echo "   Starting bot..."
    nohup python3 bot.py > bot.log 2>&1 &
    sleep 2
    if pgrep -f "python.*bot.py" >/dev/null; then
        echo "   ✓ Bot started"
    else
        echo "   ✗ Failed to start bot"
    fi
fi

echo ""
echo "=== Fix Complete ==="
echo ""
echo "Current status:"
if pgrep -f "python.*bot.py" >/dev/null 2>&1 || systemctl is-active telegram-bot >/dev/null 2>&1; then
    echo "✅ Bot is RUNNING"
    echo ""
    echo "To view logs:"
    echo "  journalctl -u telegram-bot -f  (if using systemd)"
    echo "  tail -f bot.log                (if running manually)"
else
    echo "❌ Bot is NOT running"
    echo ""
    echo "To start manually:"
    echo "  cd $(pwd)"
    echo "  python3 bot.py"
fi
echo ""
