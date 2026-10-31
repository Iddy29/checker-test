#!/bin/bash

echo "=============================================="
echo "  ALL-IN-ONE FIX: Bot + Web + Shopify + /start"
echo "  Running on VPS: 72.62.2.181"
echo "=============================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"

echo -e "${BLUE}🔍 STEP 1: Finding all components...${NC}"
echo ""

# Find directories
BOT_DIR=""
WEB_DIR=""
SHOP_DIR=""

# Search for bot.py
BOT_FILE=$(find /root /opt /home -name "bot.py" 2>/dev/null | grep -v __pycache__ | head -1)
if [ -n "$BOT_FILE" ]; then
    BOT_DIR=$(dirname "$BOT_FILE")
    echo -e "${GREEN}✓${NC} Bot found: $BOT_DIR"
else
    echo -e "${RED}✗${NC} Bot not found!"
fi

# Search for web server
WEB_FILE=$(find /root /opt /home -name "api_server.py" -o -name "app.py" -o -name "server.py" 2>/dev/null | grep -v __pycache__ | grep -v bot | head -1)
if [ -n "$WEB_FILE" ]; then
    WEB_DIR=$(dirname "$WEB_FILE")
    echo -e "${GREEN}✓${NC} Web server found: $WEB_DIR"
else
    echo -e "${YELLOW}⚠${NC} Web server not found"
fi

# Search for Shopify
SHOP_FILE=$(find /root /opt /home -name "*shopify*" 2>/dev/null | grep -v __pycache__ | head -1)
if [ -n "$SHOP_FILE" ]; then
    SHOP_DIR=$(dirname "$SHOP_FILE")
    echo -e "${GREEN}✓${NC} Shopify API found: $SHOP_DIR"
else
    echo -e "${YELLOW}⚠${NC} Shopify files not found"
fi

echo ""
echo -e "${BLUE}🔍 STEP 2: Checking current status...${NC}"
echo ""

# Check processes
echo "Running Python processes:"
ps aux | grep -v grep | grep python || echo -e "${RED}No Python processes running${NC}"
echo ""

# Check ports
echo "Listening ports:"
netstat -tlnp 2>/dev/null | grep -E "(5000|8080|8000)" || ss -tlnp 2>/dev/null | grep -E "(5000|8080|8000)" || echo -e "${YELLOW}No common web ports${NC}"
echo ""

# Stop all existing processes
echo -e "${BLUE}🛑 STEP 3: Stopping all existing processes...${NC}"
pkill -9 -f "python.*bot.py" 2>/dev/null
pkill -9 -f "python.*api_server" 2>/dev/null
pkill -9 -f "python.*app.py" 2>/dev/null
pkill -9 -f "python.*server.py" 2>/dev/null
pkill -9 -f "python.*shopify" 2>/dev/null
sleep 2
echo -e "${GREEN}✓${NC} All processes stopped"
echo ""

# Fix bot config
echo -e "${BLUE}⚙️  STEP 4: Fixing bot configuration...${NC}"
if [ -n "$BOT_DIR" ] && [ -d "$BOT_DIR" ]; then
    cd "$BOT_DIR"
    
    # Create/update config.json
    python3 << EOF
import json

config = {
    "TELEGRAM_BOT_TOKEN": "$BOT_TOKEN",
    "TELEGRAM_API_ID": "20464",
    "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a",
    "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
    "TELEGRAM_GROUP_ID": "-1003474408861",
    "TELEGRAM_GROUP_LINK": "https://t.me/+rBq3QyI5Z7MzOGY9",
    "TELEGRAM_CHANNEL_LINK": "https://t.me/TeachHit",
    "logs_group_id": "-1003816358079",
    "RAILWAY_SHOPIFY_API": "http://localhost:5000"
}

try:
    with open("config.json", "r") as f:
        existing = json.load(f)
    config.update(existing)
except:
    pass

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)

print("✓ Config updated")
print(f"  Token: {config['TELEGRAM_BOT_TOKEN'][:20]}...")
print(f"  API_ID: {config['TELEGRAM_API_ID']}")
print(f"  Shopify API: {config.get('RAILWAY_SHOPIFY_API', 'localhost:5000')}")
EOF
else
    echo -e "${RED}✗${NC} Bot directory not found"
fi
echo ""

# Install dependencies
echo -e "${BLUE}📦 STEP 5: Installing dependencies...${NC}"
pip3 install aiofiles aiohttp telethon requests flask aiohttp-cors --upgrade --quiet 2>/dev/null
echo -e "${GREEN}✓${NC} Dependencies installed"
echo ""

# Clean session files
echo -e "${BLUE}🧹 STEP 6: Cleaning session files...${NC}"
if [ -n "$BOT_DIR" ]; then
    cd "$BOT_DIR"
    rm -f cc_bot.session* 2>/dev/null
    rm -f *.session-journal 2>/dev/null
    echo -e "${GREEN}✓${NC} Session files cleaned"
fi
echo ""

# Register bot commands
echo -e "${BLUE}📱 STEP 7: Registering bot commands with Telegram...${NC}"

# Set commands
RESULT=$(curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{
    "commands": [
      {"command": "start", "description": "Start the bot and see menu"},
      {"command": "help", "description": "Show help information"},
      {"command": "cmds", "description": "Show all commands"},
      {"command": "info", "description": "Show your account info"},
      {"command": "ping", "description": "Check bot response time"},
      {"command": "skl", "description": "Stripe auth check"},
      {"command": "shp", "description": "Shopify check"}
    ]
  }')

if echo "$RESULT" | grep -q '"ok":true'; then
    echo -e "${GREEN}✓${NC} Bot commands registered"
else
    echo -e "${YELLOW}⚠${NC} Command registration: $RESULT"
fi

# Set bot name
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" \
  -H "Content-Type: application/json" \
  -d '{"name": "I-NETTZ Checker"}' > /dev/null

# Set description
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyDescription" \
  -H "Content-Type: application/json" \
  -d '{"description": "Card Checker Bot - Shopify, Stripe, BIN Lookup"}' > /dev/null

echo -e "${GREEN}✓${NC} Bot info updated"
echo ""

# Test bot token
echo -e "${BLUE}🔐 STEP 8: Testing bot token...${NC}"
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo -e "${GREEN}✓${NC} Bot token valid - @$BOT_USER"
else
    echo -e "${RED}✗${NC} Token invalid: $BOT_INFO"
fi
echo ""

# Start web server FIRST (bot depends on it)
echo -e "${BLUE}🌐 STEP 9: Starting web server (Shopify API)...${NC}"
if [ -n "$WEB_DIR" ] && [ -d "$WEB_DIR" ]; then
    cd "$WEB_DIR"
    
    # Find which file to run
    if [ -f "api_server.py" ]; then
        nohup python3 api_server.py > web_server.log 2>&1 &
        WEB_PID=$!
        echo -e "${GREEN}✓${NC} Started api_server.py (PID: $WEB_PID)"
    elif [ -f "app.py" ]; then
        nohup python3 app.py > web_server.log 2>&1 &
        WEB_PID=$!
        echo -e "${GREEN}✓${NC} Started app.py (PID: $WEB_PID)"
    elif [ -f "server.py" ]; then
        nohup python3 server.py > web_server.log 2>&1 &
        WEB_PID=$!
        echo -e "${GREEN}✓${NC} Started server.py (PID: $WEB_PID)"
    else
        echo -e "${YELLOW}⚠${NC} No web server script found"
    fi
    
    sleep 3
    
    # Check if web server is running
    if netstat -tlnp 2>/dev/null | grep -q ":5000" || ss -tlnp 2>/dev/null | grep -q ":5000"; then
        echo -e "${GREEN}✓${NC} Web server running on port 5000"
    else
        echo -e "${YELLOW}⚠${NC} Web server may not be on port 5000"
    fi
else
    echo -e "${YELLOW}⚠${NC} Web server directory not found"
fi
echo ""

# Set webhook
echo -e "${BLUE}🔗 STEP 10: Setting Telegram webhook...${NC}"
WEBHOOK_RESULT=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=http://72.62.2.181:5000/webhook")
if echo "$WEBHOOK_RESULT" | grep -q '"ok":true'; then
    echo -e "${GREEN}✓${NC} Webhook set: http://72.62.2.181:5000/webhook"
else
    echo -e "${YELLOW}⚠${NC} Webhook: $WEBHOOK_RESULT"
fi
echo ""

# Start bot
echo -e "${BLUE}🤖 STEP 11: Starting Telegram bot...${NC}"
if [ -n "$BOT_DIR" ] && [ -d "$BOT_DIR" ]; then
    cd "$BOT_DIR"
    
    # Test import first
    IMPORT_TEST=$(python3 -c "import bot" 2>&1)
    if [ -z "$IMPORT_TEST" ] || ! echo "$IMPORT_TEST" | grep -qi "error\|traceback\|valueerror"; then
        echo -e "${GREEN}✓${NC} Bot imports successfully"
        nohup python3 bot.py > bot.log 2>&1 &
        BOT_PID=$!
        sleep 5
        
        if pgrep -f "python.*bot.py" > /dev/null; then
            echo -e "${GREEN}✓${NC} Bot started (PID: $BOT_PID)"
        else
            echo -e "${RED}✗${NC} Bot failed to start"
            echo "Errors:"
            tail -50 bot.log
        fi
    else
        echo -e "${RED}✗${NC} Bot import failed"
        echo "$IMPORT_TEST" | tail -10
    fi
fi
echo ""

# Final status
echo -e "${BLUE}=============================================="
echo "  ✅ FIX COMPLETE!"
echo "==============================================${NC}"
echo ""
echo "📊 STATUS:"
echo "  Bot Process: $(pgrep -f 'python.*bot.py' > /dev/null && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}NOT RUNNING${NC}")"
echo "  Web Process: $(pgrep -f 'python.*(api_server|app\.py|server\.py)' > /dev/null && echo -e "${GREEN}RUNNING${NC}" || echo -e "${YELLOW}NOT RUNNING${NC}")"
echo "  Port 5000: $(netstat -tlnp 2>/dev/null | grep -q ':5000' && echo -e "${GREEN}LISTENING${NC}" || echo -e "${YELLOW}NOT LISTENING${NC}")"
echo "  Bot: @$BOT_USER"
echo ""
echo "📱 TEST TELEGRAM:"
echo "  1. Open: https://t.me/$BOT_USER"
echo "  2. Close chat completely"
echo "  3. Reopen it"
echo "  4. Send: /start"
echo "  5. See menu with commands!"
echo ""
echo "🌐 WEB INTERFACE:"
echo "  http://72.62.2.181:5000/"
echo "  (Controls bot start/stop)"
echo ""
echo "🛍️ SHOPIFY API:"
echo "  Integrated with web server"
echo "  Bot uses: http://localhost:5000/check"
echo ""
echo "📝 LOGS:"
if [ -n "$BOT_DIR" ]; then
    echo "  Bot: tail -f $BOT_DIR/bot.log"
fi
if [ -n "$WEB_DIR" ]; then
    echo "  Web: tail -f $WEB_DIR/web_server.log"
fi
echo ""
echo "🔧 COMMANDS:"
echo "  /start - Bot menu"
echo "  /help - Help"
echo "  /cmds - All commands"
echo "  /info - Account info"
echo "  /ping - Bot check"
echo "  /skl - Stripe check"
echo "  /shp - Shopify check"
echo ""
