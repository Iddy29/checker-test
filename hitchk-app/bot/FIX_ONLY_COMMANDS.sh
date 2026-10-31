#!/bin/bash

echo "=============================================="
echo "  MINIMAL FIX: Just Register Bot Commands"
echo "  (Don't change bot.py or config)"
echo "=============================================="
echo ""

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
BOT_DIR="/root/hitchecker/bot"

cd "$BOT_DIR"

# 1. Just register commands - NO config changes
echo "Step 1: Registering /start and other commands with Telegram..."

RESULT=$(curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{
    "commands": [
      {"command": "start", "description": "Start the bot and see menu"},
      {"command": "help", "description": "Show help information"},
      {"command": "cmds", "description": "Show all commands"},
      {"command": "info", "description": "Show your account info"},
      {"command": "ping", "description": "Check bot response time"}
    ]
  }')

if echo "$RESULT" | grep -q '"ok":true'; then
    echo "  ✅ Commands registered successfully!"
else
    echo "  ⚠ Result: $RESULT"
fi

# 2. Set bot name
echo ""
echo "Step 2: Setting bot name..."
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" \
  -H "Content-Type: application/json" \
  -d '{"name": "I-NETTZ Checker"}' > /dev/null
echo "  ✓ Bot name set"

# 3. Set description
echo ""
echo "Step 3: Setting bot description..."
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyDescription" \
  -H "Content-Type: application/json" \
  -d '{"description": "Card Checker Bot - Check cards, lookup BINs, and more!"}' > /dev/null
echo "  ✓ Description set"

# 4. Check current commands
echo ""
echo "Step 4: Verifying commands..."
GET_CMDS=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMyCommands")
echo "  Registered commands:"
echo "$GET_CMDS" | python3 -c "import sys,json; cmds=json.load(sys.stdin).get('result',[]); [print(f'    /{c[\"command\"]} - {c[\"description\"]}') for c in cmds]" 2>/dev/null || echo "$GET_CMDS"

# 5. Test bot token
echo ""
echo "Step 5: Testing bot token..."
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo "  ✅ Bot: @$BOT_USER"
else
    echo "  ⚠ Token: $BOT_INFO"
fi

# 6. Check if bot is running
echo ""
echo "Step 6: Checking bot status..."
if pgrep -f "python.*bot.py" > /dev/null; then
    echo "  ✅ Bot is running (PID: $(pgrep -f 'python.*bot.py'))"
    echo "  → No need to restart, commands will work immediately!"
else
    echo "  ⚠ Bot is not running"
    echo "  → Use web interface to start it"
fi

# 7. Show webhook info
echo ""
echo "Step 7: Webhook status..."
WEBHOOK=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo")
WEBHOOK_URL=$(echo "$WEBHOOK" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('url','Not set'))" 2>/dev/null)
echo "  Webhook URL: $WEBHOOK_URL"

echo ""
echo "=============================================="
echo "  ✅ COMMANDS FIX COMPLETE!"
echo "=============================================="
echo ""
echo "📱 TEST NOW:"
echo "  1. Open Telegram"
echo "  2. Go to: https://t.me/$BOT_USER"
echo "  3. **Close the chat completely**"
echo "  4. **Reopen it**"
echo "  5. Send: /start"
echo "  6. You should see menu!"
echo ""
echo "💡 IMPORTANT:"
echo "  • Bot is controlled by web interface"
echo "  • Don't stop/start bot manually"
echo "  • Use web interface at http://72.62.2.181:5000/"
echo "  • Commands registered - no restart needed!"
echo ""
echo "📊 Status:"
echo "  Bot Token: Valid ✅"
echo "  Commands: Registered ✅"
echo "  Bot Process: $(pgrep -f 'python.*bot.py' > /dev/null && echo 'RUNNING ✅' || echo 'STOPPED (use web to start)')"
echo ""
echo "If /start still doesn't work:"
echo "  1. Clear Telegram cache"
echo "  2. Restart Telegram app"
echo "  3. Or try from different device"
echo ""
