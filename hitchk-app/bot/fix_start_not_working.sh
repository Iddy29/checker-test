#!/bin/bash

# ============================================
# Quick Fix for Bot Not Responding to /start
# Run this on your VPS
# ============================================

echo "=============================================="
echo "  Fixing Bot - /start Command Not Working"
echo "=============================================="
echo ""

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
    echo "Run: find / -name 'bot.py' 2>/dev/null | grep -v __pycache__"
    exit 1
fi

echo "Bot directory: $BOT_DIR"
cd "$BOT_DIR"

# Step 1: Stop existing bot
echo ""
echo "Step 1: Stopping existing bot..."
pkill -f "python.*bot.py" 2>/dev/null
sleep 2
if pgrep -f "python.*bot.py" >/dev/null; then
    echo "  ⚠ Bot still running, forcing stop..."
    pkill -9 -f "python.*bot.py"
fi
echo "  ✓ Bot stopped"

# Step 2: Check bot token
echo ""
echo "Step 2: Checking bot token..."
TOKEN=$(python3 -c "import json; print(json.load(open('config.json'))['TELEGRAM_BOT_TOKEN'])" 2>/dev/null)
if [ -z "$TOKEN" ]; then
    echo "  ✗ ERROR: Cannot read bot token from config.json!"
    echo "  Check config.json exists and has TELEGRAM_BOT_TOKEN"
    exit 1
fi

# Test token
RESPONSE=$(curl -s "https://api.telegram.org/bot$TOKEN/getMe")
if echo "$RESPONSE" | grep -q '"ok":true'; then
    BOT_USERNAME=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])")
    echo "  ✓ Bot token valid - @${BOT_USERNAME}"
    echo ""
    echo "  👉 Message this bot in Telegram: @${BOT_USERNAME}"
else
    echo "  ✗ Bot token INVALID!"
    echo "  Response: $RESPONSE"
    echo ""
    echo "  FIX: Get new token from @BotFather in Telegram"
    echo "  1. Open Telegram, search @BotFather"
    echo "  2. Send /mybots"
    echo "  3. Select your bot"
    echo "  4. API Development Tools → Regenerate Token"
    echo "  5. Update config.json with new token"
    exit 1
fi

# Step 3: Install dependencies
echo ""
echo "Step 3: Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests --quiet --upgrade 2>/dev/null
echo "  ✓ Dependencies installed"

# Step 4: Clean session files
echo ""
echo "Step 4: Cleaning old session files..."
rm -f cc_bot.session* 2>/dev/null
echo "  ✓ Session files cleaned"

# Step 5: Test bot import
echo ""
echo "Step 5: Testing bot import..."
if python3 -c "import bot" 2>&1 | grep -qi error; then
    echo "  ✗ Bot import failed!"
    python3 -c "import bot" 2>&1 | tail -10
    echo ""
    echo "  Fix errors above, then restart bot"
    exit 1
else
    echo "  ✓ Bot imports successfully"
fi

# Step 6: Start bot
echo ""
echo "Step 6: Starting bot..."
nohup python3 bot.py > bot.log 2>&1 &
sleep 5

# Check if running
if pgrep -f "python.*bot.py" >/dev/null; then
    echo "  ✓ Bot started successfully!"
    echo ""
    echo "  Bot PID: $(pgrep -f 'python.*bot.py')"
else
    echo "  ✗ Bot failed to start!"
    echo ""
    echo "  Errors:"
    tail -30 bot.log
    exit 1
fi

# Step 7: Show recent logs
echo ""
echo "Step 7: Recent bot logs:"
sleep 2
tail -20 bot.log | grep -E "(started|error|Error|ERROR|running)" || tail -20 bot.log

# Final status
echo ""
echo "=============================================="
echo "  ✅ Bot is NOW RUNNING!"
echo "=============================================="
echo ""
echo "👉 TEST IN TELEGRAM:"
echo "   1. Open Telegram"
echo "   2. Search: @${BOT_USERNAME}"
echo "   3. Send: /start"
echo "   4. Bot should respond!"
echo ""
echo "📝 Bot URL: https://t.me/${BOT_USERNAME}?start=true"
echo ""
echo "📊 Monitor logs:"
echo "   tail -f $BOT_DIR/bot.log"
echo ""
echo "🔄 Restart bot:"
echo "   pkill -f 'python.*bot.py' && cd $BOT_DIR && nohup python3 bot.py > bot.log 2>&1 &"
echo ""
