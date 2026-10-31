#!/bin/bash

# ============================================
# Fix Bot with CORRECT Token
# Token: 8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo
# ============================================

echo "=============================================="
echo "  Fixing Bot with Correct Token"
echo "=============================================="
echo ""

CORRECT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"

# Find bot directory
BOT_DIR=""
for dir in "/opt/telegram-bot" "/root/telegram-bot" "$HOME/telegram-bot" "$HOME/bot"; do
    if [ -d "$dir" ] && [ -f "$dir/bot.py" ]; then
        BOT_DIR="$dir"
        break
    fi
done

if [ -z "$BOT_DIR" ]; then
    echo "ERROR: Cannot find bot directory!"
    echo "Looking for bot.py..."
    find / -name "bot.py" 2>/dev/null | grep -v __pycache__ | head -5
    exit 1
fi

echo "✓ Found bot directory: $BOT_DIR"
cd "$BOT_DIR"

# Step 1: Update config.json with correct token
echo ""
echo "Step 1: Updating config.json with correct token..."
if [ -f "config.json" ]; then
    # Backup first
    cp config.json config.json.backup.$(date +%s)
    
    # Update token using Python (safer than sed)
    python3 << PYUPDATE
import json

with open("config.json", "r") as f:
    config = json.load(f)

config["TELEGRAM_BOT_TOKEN"] = "$CORRECT_TOKEN"

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)

print("  ✓ Token updated in config.json")
PYUPDATE
else
    echo "  ✗ config.json not found! Creating..."
    cat > config.json << EOF
{
  "TELEGRAM_BOT_TOKEN": "$CORRECT_TOKEN",
  "TELEGRAM_API_ID": "12340097",
  "TELEGRAM_API_HASH": "76ad4197b5db92f328e02d9000675a10",
  "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
  "TELEGRAM_GROUP_ID": "-1003474408861",
  "TELEGRAM_GROUP_LINK": "https://t.me/+rBq3QyI5Z7MzOGY9",
  "TELEGRAM_CHANNEL_LINK": "https://t.me/TeachHit",
  "TELEGRAM_CHANNEL_ID": "@TeachHit",
  "logs_group_id": "-1003816358079"
}
EOF
    echo "  ✓ config.json created"
fi

# Step 2: Verify token is correct
echo ""
echo "Step 2: Verifying bot token..."
TOKEN_CHECK=$(python3 -c "import json; print(json.load(open('config.json'))['TELEGRAM_BOT_TOKEN'])")
if [ "$TOKEN_CHECK" = "$CORRECT_TOKEN" ]; then
    echo "  ✓ Token is correct in config.json"
else
    echo "  ✗ Token mismatch!"
    echo "  Expected: $CORRECT_TOKEN"
    echo "  Found: $TOKEN_CHECK"
fi

# Test token with Telegram API
echo ""
echo "Step 3: Testing token with Telegram API..."
RESPONSE=$(curl -s "https://api.telegram.org/bot$CORRECT_TOKEN/getMe")

if echo "$RESPONSE" | grep -q '"ok":true'; then
    BOT_USERNAME=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])")
    BOT_FIRSTNAME=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['first_name'])")
    echo "  ✅ Token is VALID!"
    echo ""
    echo "  Bot Name: $BOT_FIRSTNAME"
    echo "  Bot Username: @$BOT_USERNAME"
    echo ""
    echo "  👉 Message this bot: https://t.me/$BOT_USERNAME"
else
    echo "  ✗ Token is INVALID!"
    echo "  Response: $RESPONSE"
    echo ""
    echo "  Possible issues:"
    echo "  1. Token is wrong"
    echo "  2. Token was revoked"
    echo "  3. Bot was deleted"
    echo ""
    echo "  Get new token from @BotFather in Telegram"
    exit 1
fi

# Step 4: Stop old bot
echo ""
echo "Step 4: Stopping old bot process..."
pkill -f "python.*bot.py" 2>/dev/null
sleep 2
if pgrep -f "python.*bot.py" >/dev/null; then
    echo "  ⚠ Forcing stop..."
    pkill -9 -f "python.*bot.py"
fi
echo "  ✓ Old bot stopped"

# Step 5: Clean session files (old token session)
echo ""
echo "Step 5: Cleaning old session files..."
rm -f cc_bot.session* 2>/dev/null
rm -f *.session-journal 2>/dev/null
echo "  ✓ Session files cleaned"

# Step 6: Install dependencies
echo ""
echo "Step 6: Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests --quiet --upgrade 2>/dev/null
echo "  ✓ Dependencies installed"

# Step 7: Test bot import
echo ""
echo "Step 7: Testing bot import..."
if python3 -c "import bot" 2>&1 | grep -qi "error"; then
    echo "  ✗ Import failed!"
    python3 -c "import bot" 2>&1 | tail -10
    echo ""
    echo "  Fix errors above first"
    exit 1
else
    echo "  ✓ Bot imports successfully"
fi

# Step 8: Start bot with correct token
echo ""
echo "Step 8: Starting bot with correct token..."
nohup python3 bot.py > bot.log 2>&1 &
sleep 5

# Check if running
if pgrep -f "python.*bot.py" >/dev/null; then
    echo "  ✅ Bot started successfully!"
    echo ""
    echo "  Bot PID: $(pgrep -f 'python.*bot.py')"
else
    echo "  ✗ Bot failed to start!"
    echo ""
    echo "  Errors:"
    tail -50 bot.log
    exit 1
fi

# Step 9: Show logs
echo ""
echo "Step 9: Bot startup logs:"
sleep 2
tail -30 bot.log | grep -E "(started|Started|running|Running|error|Error|ERROR)" || tail -30 bot.log

# Final status
echo ""
echo "=============================================="
echo "  ✅ SUCCESS! Bot is RUNNING"
echo "=============================================="
echo ""
echo "🤖 Bot Information:"
echo "   Name: $BOT_FIRSTNAME"
echo "   Username: @$BOT_USERNAME"
echo "   Token: $CORRECT_TOKEN"
echo ""
echo "📱 Test Your Bot:"
echo "   1. Click: https://t.me/$BOT_USERNAME"
echo "   2. Or search @$BOT_USERNAME in Telegram"
echo "   3. Send: /start"
echo "   4. Bot should respond!"
echo ""
echo "📊 Monitor Logs:"
echo "   tail -f $BOT_DIR/bot.log"
echo ""
echo "🔄 Restart Bot:"
echo "   pkill -f 'python.*bot.py' && cd $BOT_DIR && nohup python3 bot.py > bot.log 2>&1 &"
echo ""
echo "💾 Backup Created:"
echo "   config.json.backup.*"
echo ""
