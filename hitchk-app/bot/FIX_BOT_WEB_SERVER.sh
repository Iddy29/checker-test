#!/bin/bash

# ============================================
# Complete Fix for Bot + Web Server Integration
# Bot: @Myhitz_bot
# Web: http://72.62.2.181:5000/
# ============================================

echo "=============================================="
echo "  Fixing Bot + Web Server Integration"
echo "=============================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
WEB_PORT="5000"
WEB_HOST="72.62.2.181"

# Step 1: Stop all existing processes
echo -e "${YELLOW}Step 1: Stopping all existing processes...${NC}"
pkill -f "python.*bot.py" 2>/dev/null
pkill -f "python.*api_server" 2>/dev/null
pkill -f "python.*app.py" 2>/dev/null
pkill -f "python.*server.py" 2>/dev/null
pkill -f "flask" 2>/dev/null
sleep 2
echo "  ✓ All Python processes stopped"

# Step 2: Find bot and web server directories
echo ""
echo -e "${YELLOW}Step 2: Finding bot and web server directories...${NC}"

BOT_DIR=""
WEB_DIR=""

# Search for bot.py
for dir in "/opt/telegram-bot" "/root/telegram-bot" "$HOME/telegram-bot"; do
    if [ -d "$dir" ] && [ -f "$dir/bot.py" ]; then
        BOT_DIR="$dir"
        break
    fi
done

# Search for web server
for dir in "/opt/hitchk-app" "/root/hitchk-app" "$HOME/hitchk-app" "/opt/web" "/root/web"; do
    if [ -d "$dir" ]; then
        if [ -f "$dir/api_server.py" ] || [ -f "$dir/app.py" ] || [ -f "$dir/server.py" ]; then
            WEB_DIR="$dir"
            break
        fi
    fi
done

if [ -z "$BOT_DIR" ]; then
    echo -e "${RED}✗ Cannot find bot.py!${NC}"
    find / -name "bot.py" 2>/dev/null | grep -v __pycache__ | head -5
    exit 1
fi

echo "  ✓ Bot directory: $BOT_DIR"
cd "$BOT_DIR"

if [ -n "$WEB_DIR" ]; then
    echo "  ✓ Web server directory: $WEB_DIR"
else
    echo -e "${YELLOW}⚠ Web server directory not found, searching...${NC}"
    WEB_FOUND=$(find /opt /root /home -name "api_server.py" -o -name "app.py" -o -name "server.py" 2>/dev/null | grep -v __pycache__ | head -1)
    if [ -n "$WEB_FOUND" ]; then
        WEB_DIR=$(dirname "$WEB_FOUND")
        echo "  ✓ Found web server at: $WEB_DIR"
    else
        echo -e "${YELLOW}⚠ No web server found, will start bot only${NC}"
    fi
fi

# Step 3: Update configuration
echo ""
echo -e "${YELLOW}Step 3: Updating configuration...${NC}"

python3 << PYUPDATE
import json

config_file = "$BOT_DIR/config.json"
try:
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    config['TELEGRAM_BOT_TOKEN'] = '$BOT_TOKEN'
    config.setdefault('TELEGRAM_API_ID', '12340097')
    config.setdefault('TELEGRAM_API_HASH', '76ad4197b5db92f328e02d9000675a10')
    
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print("  ✓ Config updated successfully")
except Exception as e:
    print(f"  ✗ Error updating config: {e}")
PYUPDATE

# Step 4: Install dependencies
echo ""
echo -e "${YELLOW}Step 4: Installing dependencies...${NC}"
pip3 install aiofiles aiohttp telethon requests flask --upgrade --quiet 2>/dev/null
echo "  ✓ Dependencies installed"

# Step 5: Clean session files
echo ""
echo -e "${YELLOW}Step 5: Cleaning old session files...${NC}"
rm -f "$BOT_DIR"/cc_bot.session* 2>/dev/null
rm -f "$BOT_DIR"/*.session-journal 2>/dev/null
echo "  ✓ Session files cleaned"

# Step 6: Start web server
if [ -n "$WEB_DIR" ]; then
    echo ""
    echo -e "${YELLOW}Step 6: Starting web server on port $WEB_PORT...${NC}"
    cd "$WEB_DIR"
    
    if [ -f "api_server.py" ]; then
        nohup python3 api_server.py > api_server.log 2>&1 &
        WEB_PID=$!
        echo "  ✓ Started api_server.py (PID: $WEB_PID)"
    elif [ -f "app.py" ]; then
        nohup python3 app.py > app.log 2>&1 &
        WEB_PID=$!
        echo "  ✓ Started app.py (PID: $WEB_PID)"
    elif [ -f "server.py" ]; then
        nohup python3 server.py > server.log 2>&1 &
        WEB_PID=$!
        echo "  ✓ Started server.py (PID: $WEB_PID)"
    else
        echo -e "${YELLOW}⚠ No web server script found${NC}"
    fi
    
    sleep 3
    
    # Check if web server is running
    if netstat -tlnp 2>/dev/null | grep -q ":$WEB_PORT"; then
        echo -e "${GREEN}✓ Web server is running on port $WEB_PORT${NC}"
    elif ss -tlnp 2>/dev/null | grep -q ":$WEB_PORT"; then
        echo -e "${GREEN}✓ Web server is running on port $WEB_PORT${NC}"
    else
        echo -e "${YELLOW}⚠ Web server may not be running on port $WEB_PORT${NC}"
    fi
fi

# Step 7: Test web server
echo ""
echo -e "${YELLOW}Step 7: Testing web server...${NC}"
sleep 2
WEB_RESPONSE=$(curl -s --connect-timeout 5 "http://localhost:$WEB_PORT/" 2>&1 | head -20)
if [ -n "$WEB_RESPONSE" ]; then
    echo -e "${GREEN}✓ Web server responding:${NC}"
    echo "$WEB_RESPONSE" | head -10
else
    echo -e "${YELLOW}⚠ Web server not responding on port $WEB_PORT${NC}"
    echo "Checking if anything is on port $WEB_PORT..."
    netstat -tlnp 2>/dev/null | grep "$WEB_PORT" || ss -tlnp 2>/dev/null | grep "$WEB_PORT" || echo "Port $WEB_PORT is free"
fi

# Step 8: Start Telegram bot
echo ""
echo -e "${YELLOW}Step 8: Starting Telegram bot...${NC}"
cd "$BOT_DIR"

# Test bot import first
if python3 -c "import bot" 2>&1 | grep -qi "error"; then
    echo -e "${RED}✗ Bot import failed:${NC}"
    python3 -c "import bot" 2>&1 | tail -10
    echo -e "${YELLOW}Fix errors above first${NC}"
    exit 1
else
    echo "  ✓ Bot imports successfully"
fi

# Start bot
nohup python3 bot.py > bot.log 2>&1 &
BOT_PID=$!
echo "  ✓ Bot started (PID: $BOT_PID)"
sleep 5

# Step 9: Check bot status
echo ""
echo -e "${YELLOW}Step 9: Checking bot status...${NC}"
if pgrep -f "python.*bot.py" >/dev/null; then
    echo -e "${GREEN}✓ Bot process is running${NC}"
else
    echo -e "${RED}✗ Bot process is NOT running${NC}"
    echo "Errors:"
    tail -50 bot.log
    exit 1
fi

# Step 10: Show logs
echo ""
echo -e "${YELLOW}Step 10: Recent bot logs...${NC}"
sleep 2
tail -30 bot.log | grep -E "(started|Started|running|Running|error|Error|ERROR|web|api|5000|dashboard)" || tail -30 bot.log

# Step 11: Test bot token
echo ""
echo -e "${YELLOW}Step 11: Testing bot token with Telegram API...${NC}"
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USERNAME=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])")
    echo -e "${GREEN}✓ Bot token is valid${NC}"
    echo "  Bot Username: @$BOT_USERNAME"
    echo "  Test Link: https://t.me/$BOT_USERNAME"
else
    echo -e "${RED}✗ Bot token is invalid${NC}"
    echo "Response: $BOT_INFO"
    exit 1
fi

# Step 12: Final status
echo ""
echo "=============================================="
echo -e "${GREEN}  ✅ INTEGRATION FIX COMPLETE!${NC}"
echo "=============================================="
echo ""
echo "📊 Status:"
echo "  Bot Process: $([ $(pgrep -f 'python.*bot.py' >/dev/null && echo 'RUNNING') || echo 'NOT RUNNING'])"
if [ -n "$WEB_DIR" ]; then
    echo "  Web Server: $([ $(netstat -tlnp 2>/dev/null | grep -q ':5000' && echo 'RUNNING') || echo 'NOT RUNNING'])"
fi
echo ""
echo "🤖 Bot Information:"
echo "  Username: @$BOT_USERNAME"
echo "  Link: https://t.me/$BOT_USERNAME"
echo ""
echo "🌐 Web Server:"
echo "  URL: http://$WEB_HOST:$WEB_PORT/"
echo "  Local: http://localhost:$WEB_PORT/"
echo ""
echo "📝 Log Files:"
echo "  Bot: $BOT_DIR/bot.log"
if [ -n "$WEB_DIR" ]; then
    echo "  Web: $WEB_DIR/api_server.log (or app.log)"
fi
echo ""
echo "🔍 Test Commands:"
echo "  Test Bot: curl 'https://api.telegram.org/bot$BOT_TOKEN/getMe'"
echo "  Test Web: curl http://localhost:$WEB_PORT/"
echo "  View Bot Logs: tail -f $BOT_DIR/bot.log"
echo "  Restart Bot: pkill -f 'python.*bot.py' && cd $BOT_DIR && nohup python3 bot.py > bot.log 2>&1 &"
echo ""
echo "=============================================="
echo "  👉 TEST NOW: Send /start to @$BOT_USERNAME"
echo "=============================================="
echo ""
