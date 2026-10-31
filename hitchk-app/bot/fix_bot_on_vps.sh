#!/bin/bash

# ============================================
# Telegram Bot Fix Script for VPS
# Run this on your VPS to fix the bot
# ============================================

echo "=============================================="
echo "  Telegram Bot - VPS Fix Script"
echo "=============================================="
echo ""

# Find bot directory
echo "Searching for bot directory..."
BOT_DIRS=(
    "/opt/telegram-bot"
    "/root/telegram-bot"
    "/home/telegram-bot"
    "$HOME/telegram-bot"
    "$HOME/bot"
)

BOT_DIR=""
for dir in "${BOT_DIRS[@]}"; do
    if [ -d "$dir" ] && [ -f "$dir/bot.py" ]; then
        BOT_DIR="$dir"
        break
    fi
done

if [ -z "$BOT_DIR" ]; then
    echo "ERROR: Could not find bot directory!"
    echo "Please run this script from the bot directory:"
    echo "  cd /path/to/bot"
    echo "  bash fix_bot_on_vps.sh"
    exit 1
fi

echo "✓ Found bot directory: $BOT_DIR"
cd "$BOT_DIR"

# Step 1: Backup current bot.py
echo ""
echo "Step 1: Backing up current bot.py..."
if [ -f "bot.py" ]; then
    cp bot.py "bot.py.backup.$(date +%Y%m%d_%H%M%S)"
    echo "  ✓ Backup created"
else
    echo "  ✗ bot.py not found!"
    exit 1
fi

# Step 2: Fix bot.py to load config from config.json
echo ""
echo "Step 2: Fixing bot.py configuration loading..."

# Check if the fix is already applied
if grep -q "_load_config" bot.py; then
    echo "  ✓ Configuration loading already fixed"
else
    # Create a Python script to fix bot.py
    python3 << 'PYTHON_FIX'
import re

# Read bot.py
with open("bot.py", "r") as f:
    content = f.read()

# Find and replace the configuration section
old_config = '''API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]
GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", "0"))
GROUP_LINK = os.environ.get("TELEGRAM_GROUP_LINK", "")
CHANNEL_LINK = os.environ.get("TELEGRAM_CHANNEL_LINK", "")
CHANNEL_ID_RAW = os.environ.get("TELEGRAM_CHANNEL_ID", "")'''

new_config = '''def _load_config():
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
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _config.get("TELEGRAM_BOT_TOKEN", ""))
ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", _config.get("TELEGRAM_ADMIN_ID", "")).split(",") if x.strip()]
GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", _config.get("TELEGRAM_GROUP_ID", "0")))
GROUP_LINK = os.environ.get("TELEGRAM_GROUP_LINK", _config.get("TELEGRAM_GROUP_LINK", ""))
CHANNEL_LINK = os.environ.get("TELEGRAM_CHANNEL_LINK", _config.get("TELEGRAM_CHANNEL_LINK", ""))
CHANNEL_ID_RAW = os.environ.get("TELEGRAM_CHANNEL_ID", _config.get("TELEGRAM_CHANNEL_ID", ""))'''

if old_config in content:
    content = content.replace(old_config, new_config)
    with open("bot.py", "w") as f:
        f.write(content)
    print("  ✓ Configuration loading fixed")
else:
    print("  ⚠ Could not find configuration section to fix")

# Fix the asyncio event loop policy for Python 3.14+
old_loop = "if sys.platform.startswith('win'):"
new_loop = "if sys.platform.startswith('win') and sys.version_info < (3, 14):"

if old_loop in content:
    content = content.replace(old_loop, new_loop)
    with open("bot.py", "w") as f:
        f.write(content)
    print("  ✓ Python 3.14+ compatibility fixed")
PYTHON_FIX
fi

# Step 3: Check and install dependencies
echo ""
echo "Step 3: Checking dependencies..."

# Create requirements.txt if missing
if [ ! -f "requirements.txt" ]; then
    echo "  Creating requirements.txt..."
    cat > requirements.txt << 'EOF'
telethon==1.44.0
aiohttp==3.14.1
aiofiles==25.1.0
requests==2.33.1
EOF
    echo "  ✓ requirements.txt created"
fi

# Install missing dependencies
echo "  Installing/updating dependencies..."
pip3 install -r requirements.txt --quiet --upgrade

# Check if all modules can be imported
echo ""
echo "Step 4: Testing bot imports..."
if python3 -c "import bot" 2>/dev/null; then
    echo "  ✓ Bot imports successfully!"
else
    echo "  ✗ Import failed, installing missing packages..."
    pip3 install telethon aiohttp aiofiles requests
fi

# Step 5: Check config.json
echo ""
echo "Step 5: Checking config.json..."
if [ -f "config.json" ]; then
    echo "  ✓ config.json found"
    echo "  Bot Token: $(python3 -c "import json; c=json.load(open('config.json')); print(c.get('TELEGRAM_BOT_TOKEN', 'MISSING')[:20]+'...')" 2>/dev/null || echo "ERROR reading config")"
else
    echo "  ✗ config.json NOT FOUND!"
    echo "  Please create config.json with your Telegram credentials"
fi

# Step 6: Check if bot is running
echo ""
echo "Step 6: Checking bot status..."
if systemctl is-active telegram-bot >/dev/null 2>&1; then
    echo "  ✓ Bot service is running"
    echo "  Restarting bot..."
    systemctl restart telegram-bot
    sleep 2
    systemctl status telegram-bot --no-pager | head -10
elif pgrep -f "python.*bot.py" >/dev/null 2>&1; then
    echo "  ✓ Bot process is running (not as service)"
    echo "  Consider setting up systemd service for auto-restart"
else
    echo "  ⚠ Bot is not running"
    echo "  Starting bot..."
    
    # Try to start as service first
    if [ -f "telegram-bot.service" ]; then
        cp telegram-bot.service /etc/systemd/system/ 2>/dev/null
        systemctl daemon-reload
        systemctl enable telegram-bot 2>/dev/null
        systemctl start telegram-bot 2>/dev/null
        
        if systemctl is-active telegram-bot >/dev/null 2>&1; then
            echo "  ✓ Bot started as systemd service"
        else
            echo "  Starting bot manually..."
            nohup python3 bot.py > bot.log 2>&1 &
            sleep 2
            if pgrep -f "python.*bot.py" >/dev/null; then
                echo "  ✓ Bot started in background"
            else
                echo "  ✗ Failed to start bot"
            fi
        fi
    else
        echo "  Starting bot manually..."
        nohup python3 bot.py > bot.log 2>&1 &
        sleep 2
        if pgrep -f "python.*bot.py" >/dev/null; then
            echo "  ✓ Bot started in background"
        else
            echo "  ✗ Failed to start bot"
        fi
    fi
fi

# Step 7: Show logs
echo ""
echo "Step 7: Recent bot logs:"
if [ -f "bot.log" ]; then
    echo "  From bot.log:"
    tail -20 bot.log
elif systemctl is-active telegram-bot >/dev/null 2>&1; then
    journalctl -u telegram-bot --no-pager -n 20
fi

# Final status
echo ""
echo "=============================================="
echo "  Fix Complete!"
echo "=============================================="
echo ""
echo "Bot Directory: $BOT_DIR"
echo ""

if pgrep -f "python.*bot.py" >/dev/null 2>&1 || systemctl is-active telegram-bot >/dev/null 2>&1; then
    echo "✅ Bot is RUNNING"
    echo ""
    echo "Useful commands:"
    echo "  View logs:     journalctl -u telegram-bot -f"
    echo "  Restart:       systemctl restart telegram-bot"
    echo "  Stop:          systemctl stop telegram-bot"
    echo "  Status:        systemctl status telegram-bot"
    echo ""
    echo "Test your bot in Telegram:"
    echo "  1. Open Telegram"
    echo "  2. Search for your bot"
    echo "  3. Send /start"
else
    echo "❌ Bot is NOT running"
    echo ""
    echo "Check errors:"
    echo "  cd $BOT_DIR"
    echo "  python3 bot.py"
fi

echo ""
