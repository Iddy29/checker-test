#!/bin/bash

echo "=============================================="
echo "  DIAGNOSTIC - Finding Exact Problem"
echo "=============================================="
echo ""

BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"

# 1. Check bot process
echo "1️⃣ Is bot.py running?"
if pgrep -f "python.*bot.py" > /dev/null; then
    echo "   ✅ YES - Bot process is running"
    ps aux | grep -v grep | grep bot.py
else
    echo "   ❌ NO - Bot process is NOT running"
    echo "   → This is the problem!"
fi

# 2. Check web server
echo ""
echo "2️⃣ Is web server running on port 5000?"
if netstat -tlnp 2>/dev/null | grep -q ":5000" || ss -tlnp 2>/dev/null | grep -q ":5000"; then
    echo "   ✅ YES - Web server is running"
    netstat -tlnp 2>/dev/null | grep 5000 || ss -tlnp 2>/dev/null | grep 5000
else
    echo "   ❌ NO - Web server is NOT running on port 5000"
fi

# 3. Test web server
echo ""
echo "3️⃣ Testing web server response:"
WEB_RESP=$(curl -s --connect-timeout 3 http://localhost:5000/ 2>&1)
if [ -n "$WEB_RESP" ]; then
    echo "   ✅ Web server responds"
    echo "   Response: $WEB_RESP" | head -20
else
    echo "   ❌ Web server does NOT respond"
fi

# 4. Check bot token
echo ""
echo "4️⃣ Testing bot token:"
BOT_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    echo "   ✅ Token is VALID"
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo "   Bot: @$BOT_USER"
else
    echo "   ❌ Token is INVALID"
    echo "   Response: $BOT_INFO"
fi

# 5. Check webhook
echo ""
echo "5️⃣ Checking webhook:"
WEBHOOK_INFO=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo")
WEBHOOK_URL=$(echo "$WEBHOOK_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('url','Not set'))" 2>/dev/null)
echo "   Webhook URL: $WEBHOOK_URL"
if echo "$WEBHOOK_URL" | grep -q "72.62.2.181"; then
    echo "   ✅ Webhook points to correct server"
else
    echo "   ⚠️  Webhook may not be set correctly"
fi

# 6. Find bot directory
echo ""
echo "6️⃣ Finding bot directory:"
BOT_DIR=$(find /opt /root /home -name "bot.py" 2>/dev/null | grep -v __pycache__ | head -1 | xargs dirname)
if [ -n "$BOT_DIR" ]; then
    echo "   Found: $BOT_DIR"
    cd "$BOT_DIR"
    
    # Check config
    echo ""
    echo "7️⃣ Checking config.json:"
    if [ -f "config.json" ]; then
        CONFIG_TOKEN=$(python3 -c "import json; print(json.load(open('config.json')).get('TELEGRAM_BOT_TOKEN','NOT FOUND'))" 2>/dev/null)
        if [ "$CONFIG_TOKEN" = "$BOT_TOKEN" ]; then
            echo "   ✅ Token in config is CORRECT"
        else
            echo "   ❌ Token in config is WRONG or MISSING"
            echo "   Current: $CONFIG_TOKEN"
            echo "   Expected: $BOT_TOKEN"
        fi
    else
        echo "   ❌ config.json NOT FOUND"
    fi
else
    echo "   ❌ bot.py NOT FOUND anywhere!"
fi

# 8. Check bot logs
echo ""
echo "8️⃣ Checking bot logs:"
if [ -f "$BOT_DIR/bot.log" ]; then
    echo "   Last 20 lines:"
    tail -20 "$BOT_DIR/bot.log"
else
    echo "   ⚠️  No bot.log found"
fi

# 9. Try to import bot
echo ""
echo "9️⃣ Testing bot import:"
cd "$BOT_DIR" 2>/dev/null
IMPORT_TEST=$(python3 -c "import bot" 2>&1)
if [ -z "$IMPORT_TEST" ]; then
    echo "   ✅ Bot imports successfully"
else
    echo "   ❌ Bot import FAILED"
    echo "   Error: $IMPORT_TEST"
fi

# SUMMARY
echo ""
echo "=============================================="
echo "  DIAGNOSIS COMPLETE"
echo "=============================================="
echo ""

# Determine the problem
PROBLEMS=""

if ! pgrep -f "python.*bot.py" > /dev/null; then
    PROBLEMS="$PROBLEMS\n❌ Bot process NOT running"
fi

if ! (netstat -tlnp 2>/dev/null | grep -q ":5000" || ss -tlnp 2>/dev/null | grep -q ":5000"); then
    PROBLEMS="$PROBLEMS\n❌ Web server NOT running on port 5000"
fi

CONFIG_TOKEN=$(python3 -c "import json; print(json.load(open('$BOT_DIR/config.json')).get('TELEGRAM_BOT_TOKEN',''))" 2>/dev/null)
if [ "$CONFIG_TOKEN" != "$BOT_TOKEN" ]; then
    PROBLEMS="$PROBLEMS\n❌ Wrong token in config.json"
fi

if [ -n "$IMPORT_TEST" ]; then
    PROBLEMS="$PROBLEMS\n❌ Bot has import errors"
fi

if [ -z "$PROBLEMS" ]; then
    echo "✅ Everything looks OK!"
    echo "   Bot should be working..."
    echo ""
    echo "Try: https://t.me/Myhitz_bot"
    echo "Send: /start"
else
    echo "PROBLEMS FOUND:"
    echo -e "$PROBLEMS"
    echo ""
    echo "=============================================="
    echo "  APPLYING FIXES..."
    echo "=============================================="
    echo ""
    
    # Fix 1: Update config
    echo "Fix 1: Updating config.json..."
    python3 << EOF
import json
with open("$BOT_DIR/config.json", 'r') as f:
    config = json.load(f)
config['TELEGRAM_BOT_TOKEN'] = '$BOT_TOKEN'
with open("$BOT_DIR/config.json", 'w') as f:
    json.dump(config, f, indent=2)
print("  ✓ Config updated")
EOF
    
    # Fix 2: Install deps
    echo ""
    echo "Fix 2: Installing dependencies..."
    pip3 install aiofiles aiohttp telethon requests flask --upgrade --quiet 2>/dev/null
    echo "  ✓ Dependencies installed"
    
    # Fix 3: Clean session
    echo ""
    echo "Fix 3: Cleaning session files..."
    rm -f "$BOT_DIR"/cc_bot.session*
    echo "  ✓ Session cleaned"
    
    # Fix 4: Start web server
    echo ""
    echo "Fix 4: Starting web server..."
    WEB_DIR=$(find /opt /root /home -name "api_server.py" -o -name "app.py" 2>/dev/null | grep -v __pycache__ | head -1 | xargs dirname)
    cd "$WEB_DIR" 2>/dev/null || cd "$BOT_DIR"
    if [ -f "api_server.py" ]; then
        nohup python3 api_server.py > web_server.log 2>&1 &
        echo "  ✓ Started api_server.py"
    elif [ -f "app.py" ]; then
        nohup python3 app.py > web_server.log 2>&1 &
        echo "  ✓ Started app.py"
    fi
    sleep 3
    
    # Fix 5: Set webhook
    echo ""
    echo "Fix 5: Setting webhook..."
    curl -s "https://api.telegram.org/bot$BOT_TOKEN/deleteWebhook" > /dev/null
    sleep 1
    WEBHOOK_RESULT=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=http://72.62.2.181:5000/webhook")
    if echo "$WEBHOOK_RESULT" | grep -q '"ok":true'; then
        echo "  ✓ Webhook set successfully"
    else
        echo "  ⚠️  Webhook result: $WEBHOOK_RESULT"
    fi
    
    # Fix 6: Start bot
    echo ""
    echo "Fix 6: Starting bot..."
    cd "$BOT_DIR"
    pkill -f "python.*bot.py" 2>/dev/null
    sleep 2
    nohup python3 bot.py > bot.log 2>&1 &
    sleep 5
    
    if pgrep -f "python.*bot.py" > /dev/null; then
        echo "  ✅ Bot started successfully!"
    else
        echo "  ❌ Bot failed to start!"
        echo ""
        echo "  Errors:"
        tail -50 bot.log
    fi
fi

echo ""
echo "=============================================="
echo "  FINAL STATUS"
echo "=============================================="
echo ""
echo "Bot process: $(pgrep -f 'python.*bot.py' > /dev/null && echo '✅ RUNNING' || echo '❌ NOT RUNNING')"
echo "Web server: $(netstat -tlnp 2>/dev/null | grep -q ':5000' && echo '✅ RUNNING' || echo '❌ NOT RUNNING')"
echo ""
echo "📱 TEST NOW: https://t.me/Myhitz_bot"
echo "   Send: /start"
echo ""
echo "📊 View logs: tail -f $BOT_DIR/bot.log"
echo ""
