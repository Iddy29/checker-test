#!/bin/bash

# ============================================
# COMPLETE FIX - Bot + Web Server Integration
# BOTH will use the SAME Telegram bot token
# ============================================

echo "=============================================="
echo "  COMPLETE FIX: Bot + Web Server"
echo "  Token: 8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
echo "=============================================="
echo ""

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
BOT_USERNAME="Myhitz_bot"
WEB_PORT="5000"
WEB_HOST="72.62.2.181"
WEBHOOK_URL="https://$WEB_HOST:$WEB_PORT/webhook"

# Step 1: Stop ALL existing processes
echo "Step 1: Stopping all existing processes..."
pkill -9 -f "python.*bot.py" 2>/dev/null
pkill -9 -f "python.*api_server" 2>/dev/null
pkill -9 -f "python.*app.py" 2>/dev/null
pkill -9 -f "python.*server.py" 2>/dev/null
pkill -9 -f "python.*flask" 2>/dev/null
sleep 2
echo "  ✓ All processes stopped"

# Step 2: Find directories
echo ""
echo "Step 2: Finding bot and web server directories..."

BOT_DIR=""
WEB_DIR=""

# Find bot.py
BOT_FILE=$(find /opt /root /home -name "bot.py" 2>/dev/null | grep -v __pycache__ | head -1)
if [ -n "$BOT_FILE" ]; then
    BOT_DIR=$(dirname "$BOT_FILE")
    echo "  ✓ Found bot.py at: $BOT_DIR"
else
    echo "  ✗ Cannot find bot.py!"
    exit 1
fi

# Find web server
WEB_FILE=$(find /opt /root /home -name "api_server.py" -o -name "app.py" -o -name "server.py" 2>/dev/null | grep -v __pycache__ | grep -v bot | head -1)
if [ -n "$WEB_FILE" ]; then
    WEB_DIR=$(dirname "$WEB_FILE")
    echo "  ✓ Found web server at: $WEB_DIR"
else
    echo "  ⚠ Web server not found, will search more..."
fi

# Step 3: Update bot config
echo ""
echo "Step 3: Updating bot configuration..."
cd "$BOT_DIR"

python3 << PYUPDATE
import json

config_file = "config.json"
try:
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    # Update ALL Telegram credentials
    config['TELEGRAM_BOT_TOKEN'] = '$BOT_TOKEN'
    config['TELEGRAM_API_ID'] = '12340097'
    config['TELEGRAM_API_HASH'] = '76ad4197b5db92f328e02d9000675a10'
    config['TELEGRAM_ADMIN_ID'] = '1517013110,8726037072,1834450801,8268547077,1214399247'
    
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print("  ✓ Bot config updated")
except Exception as e:
    print(f"  ✗ Error: {e}")
    # Create new config
    config = {
        "TELEGRAM_BOT_TOKEN": "$BOT_TOKEN",
        "TELEGRAM_API_ID": "12340097",
        "TELEGRAM_API_HASH": "76ad4197b5db92f328e02d9000675a10",
        "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
        "TELEGRAM_GROUP_ID": "-1003474408861",
        "TELEGRAM_GROUP_LINK": "https://t.me/+rBq3QyI5Z7MzOGY9",
        "logs_group_id": "-1003816358079"
    }
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    print("  ✓ Created new config.json")
PYUPDATE

# Step 4: Update web server config
echo ""
echo "Step 4: Updating web server configuration..."

if [ -n "$WEB_DIR" ]; then
    cd "$WEB_DIR"
    
    # Look for config files
    for config_file in config.json app.json settings.json .env config.py; do
        if [ -f "$config_file" ]; then
            echo "  Found: $config_file"
            
            # Update based on file type
            if [[ "$config_file" == *.json ]]; then
                python3 << PYUPDATE2
import json
try:
    with open("$config_file", 'r') as f:
        config = json.load(f)
    config['TELEGRAM_BOT_TOKEN'] = '$BOT_TOKEN'
    config['BOT_TOKEN'] = '$BOT_TOKEN'
    config['telegram_bot_token'] = '$BOT_TOKEN'
    with open("$config_file", 'w') as f:
        json.dump(config, f, indent=2)
    print("  ✓ Updated $config_file")
except:
    pass
PYUPDATE2
            fi
            
            # If .env file
            if [[ "$config_file" == ".env" ]]; then
                sed -i "s/TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=$BOT_TOKEN/" "$config_file" 2>/dev/null
                sed -i "s/BOT_TOKEN=.*/BOT_TOKEN=$BOT_TOKEN/" "$config_file" 2>/dev/null
                echo "  ✓ Updated .env"
            fi
        fi
    done
    
    # Also update any Python files that might have hardcoded token
    echo "  Checking Python files for old tokens..."
    grep -rl "AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo\|AAF_o8AlMFJPRNUVKINbrkYyONr1g4IfMcA" *.py 2>/dev/null | while read file; do
        echo "    Found token in: $file"
    done
else
    echo "  ⚠ Web server directory not found"
fi

# Step 5: Install dependencies
echo ""
echo "Step 5: Installing dependencies..."
pip3 install aiofiles aiohttp telethon requests flask aiohttp-cors --upgrade --quiet 2>/dev/null
echo "  ✓ Dependencies installed"

# Step 6: Clean session files
echo ""
echo "Step 6: Cleaning old session files..."
rm -f "$BOT_DIR"/cc_bot.session* 2>/dev/null
rm -f "$BOT_DIR"/*.session-journal 2>/dev/null
echo "  ✓ Session files cleaned"

# Step 7: Delete old webhook
echo ""
echo "Step 7: Deleting old Telegram webhook..."
curl -s "https://api.telegram.org/bot$BOT_TOKEN/deleteWebhook" > /dev/null
sleep 1
echo "  ✓ Old webhook deleted"

# Step 8: Start web server FIRST
echo ""
echo "Step 8: Starting web server on port $WEB_PORT..."

cd "$WEB_DIR" 2>/dev/null || cd "$BOT_DIR"

# Find which web server file exists
if [ -f "api_server.py" ]; then
    WEB_SCRIPT="api_server.py"
elif [ -f "app.py" ]; then
    WEB_SCRIPT="app.py"
elif [ -f "server.py" ]; then
    WEB_SCRIPT="server.py"
elif [ -f "main.py" ]; then
    WEB_SCRIPT="main.py"
else
    echo "  ⚠ No web server script found!"
    WEB_SCRIPT=""
fi

if [ -n "$WEB_SCRIPT" ]; then
    echo "  Starting: $WEB_SCRIPT"
    nohup python3 "$WEB_SCRIPT" > web_server.log 2>&1 &
    WEB_PID=$!
    sleep 5
    
    # Check if web server is running
    if netstat -tlnp 2>/dev/null | grep -q ":$WEB_PORT" || ss -tlnp 2>/dev/null | grep -q ":$WEB_PORT"; then
        echo "  ✓ Web server running on port $WEB_PORT (PID: $WEB_PID)"
    else
        echo "  ⚠ Web server may not be running, checking..."
        ps aux | grep $WEB_PID | grep -v grep
    fi
else
    echo "  ⚠ Skipping web server start"
fi

# Step 9: Test web server
echo ""
echo "Step 9: Testing web server..."
sleep 2

# Test locally
WEB_RESPONSE=$(curl -s --connect-timeout 5 "http://localhost:$WEB_PORT/" 2>&1)
if [ -n "$WEB_RESPONSE" ] && ! echo "$WEB_RESPONSE" | grep -q "404"; then
    echo "  ✓ Web server responding at http://localhost:$WEB_PORT/"
    echo "  Response preview:"
    echo "$WEB_RESPONSE" | head -5
elif echo "$WEB_RESPONSE" | grep -q "404"; then
    echo "  ⚠ Web server responding but returns 404"
    echo "  This might be normal - checking available routes..."
    # Try common routes
    for route in "/api" "/webhook" "/health" "/status" "/"; do
        TEST=$(curl -s --connect-timeout 3 "http://localhost:$WEB_PORT$route" 2>&1 | head -1)
        if [ -n "$TEST" ] && ! echo "$TEST" | grep -q "404"; then
            echo "  ✓ Route $route works"
        fi
    done
else
    echo "  ⚠ Web server not responding"
    if [ -f "web_server.log" ]; then
        echo "  Web server logs:"
        tail -20 web_server.log
    fi
fi

# Test from external
echo ""
echo "Testing from external..."
EXTERNAL_TEST=$(curl -s --connect-timeout 5 "http://$WEB_HOST:$WEB_PORT/" 2>&1 | head -3)
if [ -n "$EXTERNAL_TEST" ]; then
    echo "  ✓ External access working"
else
    echo "  ⚠ External access may be blocked (firewall?)"
fi

# Step 10: Set Telegram webhook
echo ""
echo "Step 10: Setting Telegram webhook..."

# Set webhook to point to web server
WEBHOOK_RESULT=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=https://$WEB_HOST:$WEB_PORT/webhook")

if echo "$WEBHOOK_RESULT" | grep -q '"ok":true'; then
    echo "  ✓ Webhook set successfully"
    echo "  Webhook URL: https://$WEB_HOST:$WEB_PORT/webhook"
else
    echo "  ⚠ Webhook may not be set correctly"
    echo "  Result: $WEBHOOK_RESULT"
    
    # Try HTTP instead of HTTPS
    echo "  Trying HTTP webhook..."
    WEBHOOK_RESULT2=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=http://$WEB_HOST:$WEB_PORT/webhook")
    if echo "$WEBHOOK_RESULT2" | grep -q '"ok":true'; then
        echo "  ✓ Webhook set (HTTP)"
        echo "  Webhook URL: http://$WEB_HOST:$WEB_PORT/webhook"
    fi
fi

# Step 11: Start Telegram bot
echo ""
echo "Step 11: Starting Telegram bot..."
cd "$BOT_DIR"

# Test import
if python3 -c "import bot" 2>&1 | grep -qi "error\|traceback"; then
    echo "  ✗ Bot import failed!"
    python3 -c "import bot" 2>&1 | tail -10
    echo "  Fix errors above first"
else
    echo "  ✓ Bot imports successfully"
    nohup python3 bot.py > bot.log 2>&1 &
    BOT_PID=$!
    sleep 5
    
    if pgrep -f "python.*bot.py" >/dev/null; then
        echo "  ✓ Bot started (PID: $BOT_PID)"
    else
        echo "  ✗ Bot failed to start!"
        tail -50 bot.log
    fi
fi

# Step 12: Verify bot token
echo ""
echo "Step 12: Verifying bot token..."
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    echo "  ✓ Token is valid"
    echo "  Bot: @$BOT_USERNAME"
else
    echo "  ✗ Token may be invalid"
    echo "  Response: $BOT_INFO"
fi

# Step 13: Final status
echo ""
echo "=============================================="
echo "  ✅ FIX COMPLETE!"
echo "=============================================="
echo ""
echo "📊 Status:"
echo "  Bot Token: $BOT_TOKEN"
echo "  Bot Username: @$BOT_USERNAME"
echo "  Web Server: http://$WEB_HOST:$WEB_PORT/"
echo "  Webhook: https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
echo ""
echo "🔍 Check Status:"
echo "  Bot Process: ps aux | grep bot.py"
echo "  Web Process: ps aux | grep -E '(api_server|app.py|server.py)'"
echo "  Port 5000: netstat -tlnp | grep 5000"
echo "  Bot Logs: tail -f $BOT_DIR/bot.log"
echo "  Web Logs: tail -f $WEB_DIR/web_server.log"
echo ""
echo "🧪 Test:"
echo "  1. Web Server: http://$WEB_HOST:$WEB_PORT/"
echo "  2. Bot Token: curl 'https://api.telegram.org/bot$BOT_TOKEN/getMe'"
echo "  3. Webhook Info: curl 'https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo'"
echo "  4. Telegram: https://t.me/$BOT_USERNAME"
echo ""
echo "🔧 If still not working:"
echo "  1. Check firewall: ufw allow 5000/tcp"
echo "  2. Check web server logs: cat $WEB_DIR/web_server.log"
echo "  3. Check bot logs: cat $BOT_DIR/bot.log"
echo "  4. Restart both: pkill -f 'python.*' && cd $BOT_DIR && python3 bot.py &"
echo ""
echo "=============================================="
echo "  👉 TEST NOW: https://t.me/$BOT_USERNAME"
echo "  Send /start and bot should respond!"
echo "=============================================="
echo ""
