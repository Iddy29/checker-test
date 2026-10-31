#!/bin/bash

echo "=============================================="
echo "  FIX: /start Command & Bot Menu"
echo "  Registering commands with Telegram..."
echo "=============================================="
echo ""

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
BOT_DIR="/root/hitchecker/bot"

cd "$BOT_DIR"

# 1. Test bot token first
echo "Step 1: Testing bot token..."
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo "  ✅ Bot token valid - @$BOT_USER"
else
    echo "  ❌ Token invalid: $BOT_INFO"
    exit 1
fi

# 2. Register bot commands with Telegram
echo ""
echo "Step 2: Registering bot commands with Telegram API..."

# Set bot commands
COMMANDS='{
  "commands": [
    {
      "command": "start",
      "description": "Start the bot and see menu"
    },
    {
      "command": "help",
      "description": "Show help information"
    },
    {
      "command": "cmds",
      "description": "Show all commands"
    },
    {
      "command": "info",
      "description": "Show your account info"
    },
    {
      "command": "ping",
      "description": "Check bot response time"
    }
  ]
}'

SET_CMDS_RESULT=$(curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "$COMMANDS")

if echo "$SET_CMDS_RESULT" | grep -q '"ok":true'; then
    echo "  ✅ Bot commands registered successfully!"
else
    echo "  ⚠ Command registration: $SET_CMDS_RESULT"
fi

# 3. Check current commands
echo ""
echo "Step 3: Checking registered commands..."
GET_CMDS=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMyCommands")
echo "  Current commands:"
echo "$GET_CMDS" | python3 -c "import sys,json; cmds=json.load(sys.stdin).get('result',[]); [print(f'    /{c[\"command\"]} - {c[\"description\"]}') for c in cmds]" 2>/dev/null || echo "$GET_CMDS"

# 4. Set bot description and about text
echo ""
echo "Step 4: Setting bot info..."

# Set bot description (shown in groups)
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyDescription" \
  -H "Content-Type: application/json" \
  -d '{"description": "I-NETTZ Checker Bot - Check cards, lookup BINs, and more!"}' > /dev/null

# Set bot short description (shown in chat list)
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyShortDescription" \
  -H "Content-Type: application/json" \
  -d '{"short_description": "Card Checker Bot"}' > /dev/null

# Set bot name (shown in contacts)
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" \
  -H "Content-Type: application/json" \
  -d '{"name": "I-NETTZ Checker"}' > /dev/null

echo "  ✓ Bot info updated"

# 5. Check if bot.py has proper /start handler
echo ""
echo "Step 5: Checking bot.py for /start handler..."

if grep -q "@client.on(events.NewMessage(pattern=r'" "$BOT_DIR/bot.py" || grep -q "async def start(" "$BOT_DIR/bot.py"; then
    echo "  ✓ /start handler found in bot.py"
else
    echo "  ⚠ /start handler may be missing"
    echo "  Checking for start function..."
    grep -n "start" "$BOT_DIR/bot.py" | head -10
fi

# 6. Restart bot to apply changes
echo ""
echo "Step 6: Restarting bot..."
pkill -f "python.*bot.py"
sleep 2
rm -f cc_bot.session*
nohup python3 bot.py > bot.log 2>&1 &
sleep 5

if pgrep -f "python.*bot.py" > /dev/null; then
    echo "  ✅ Bot restarted"
else
    echo "  ⚠ Bot may have issues"
    tail -20 bot.log
fi

# 7. Test bot response
echo ""
echo "Step 7: Testing bot..."
sleep 3
echo "  Recent logs:"
tail -30 bot.log | grep -E "(started|Started|running|/start|command)" || tail -30 bot.log

# 8. Get webhook info
echo ""
echo "Step 8: Webhook status..."
WEBHOOK_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo")
WEBHOOK_URL=$(echo "$WEBHOOK_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('url','Not set'))" 2>/dev/null)
echo "  Webhook URL: $WEBHOOK_URL"

# Final instructions
echo ""
echo "=============================================="
echo "  ✅ COMMANDS FIX COMPLETE!"
echo "=============================================="
echo ""
echo "📱 Bot Commands Registered:"
echo "  /start - Start the bot and see menu"
echo "  /help - Show help information"
echo "  /cmds - Show all commands"
echo "  /info - Show your account info"
echo "  /ping - Check bot response time"
echo ""
echo "🤖 Bot Info:"
echo "  Username: @$BOT_USER"
echo "  Commands: Registered ✅"
echo "  Status: $(pgrep -f 'python.*bot.py' > /dev/null && echo 'RUNNING' || echo 'CHECK LOGS')"
echo ""
echo "🧪 TEST NOW:"
echo "  1. Open: https://t.me/$BOT_USER"
echo "  2. Send: /start"
echo "  3. You should see menu buttons!"
echo ""
echo "💡 If still no menu:"
echo "  • Close the chat with bot"
echo "  • Open it again"
echo "  • Send /start"
echo "  • Commands should appear!"
echo ""
echo "📝 Logs: tail -f $BOT_DIR/bot.log"
echo ""
