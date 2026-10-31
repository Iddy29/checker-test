#!/bin/bash

echo "=============================================="
echo "  COMPLETE VPS DIAGNOSTIC"
echo "  Bot + Web + Shopify API"
echo "=============================================="
echo ""

# Find all relevant directories
echo "🔍 Finding all components..."
echo ""

# 1. Find bot.py
echo "1️⃣ Bot files:"
BOT_FILES=$(find /root /opt /home -name "bot.py" 2>/dev/null | grep -v __pycache__)
echo "$BOT_FILES"
echo ""

# 2. Find Shopify API files
echo "2️⃣ Shopify API files:"
SHOP_FILES=$(find /root /opt /home -name "*shopify*" -o -name "*api*" 2>/dev/null | grep -v __pycache__ | grep -v ".pyc")
echo "$SHOP_FILES"
echo ""

# 3. Find web server
echo "3️⃣ Web server files:"
WEB_FILES=$(find /root /opt /home -name "api_server.py" -o -name "app.py" -o -name "server.py" 2>/dev/null | grep -v __pycache__)
echo "$WEB_FILES"
echo ""

# 4. Check running processes
echo "4️⃣ Running processes:"
ps aux | grep -v grep | grep -E "(python|bot|shopify|api)" || echo "No Python processes running"
echo ""

# 5. Check ports
echo "5️⃣ Listening ports:"
netstat -tlnp 2>/dev/null | grep -E "(5000|8080|80|443)" || ss -tlnp 2>/dev/null | grep -E "(5000|8080|80|443)" || echo "No common ports"
echo ""

# 6. Check hitchecker directory
echo "6️⃣ HitChecker structure:"
if [ -d "/root/hitchecker" ]; then
    echo "Root: /root/hitchecker"
    ls -la /root/hitchecker/ 2>/dev/null | head -15
    echo ""
    echo "Subdirs:"
    find /root/hitchecker -maxdepth 2 -type d 2>/dev/null
else
    echo "/root/hitchecker not found"
fi
echo ""

# 7. Check config files
echo "7️⃣ Config files:"
find /root/hitchecker -name "config.json" -o -name ".env" -o -name "settings.json" 2>/dev/null | while read cfg; do
    echo "File: $cfg"
    head -10 "$cfg" 2>/dev/null
    echo "---"
done
echo ""

# 8. Check Shopify API config
echo "8️⃣ Shopify API configuration:"
find /root/hitchecker -name "*shopify*" -type f 2>/dev/null | while read shp; do
    echo "File: $shp"
    head -20 "$shp" 2>/dev/null | grep -E "(api|key|token|url)" || head -20 "$shp"
    echo "---"
done
echo ""

# 9. Check logs
echo "9️⃣ Recent logs:"
find /root/hitchecker -name "*.log" 2>/dev/null | head -5 | while read logfile; do
    echo "=== $logfile ==="
    tail -20 "$logfile" 2>/dev/null | tail -5
    echo ""
done
echo ""

# 10. Check for process control scripts
echo "🔟 Process control scripts:"
find /root/hitchecker -name "*.sh" -o -name "*start*" -o -name "*stop*" -o -name "*restart*" 2>/dev/null | head -10
echo ""

# 11. Check systemd services
echo "1️⃣1️⃣ Systemd services:"
systemctl list-units 2>/dev/null | grep -E "(bot|telegram|shopify|api|checker)" | head -10 || echo "No relevant services"
echo ""

# 12. Check if bot is controlled by web
echo "1️⃣2️⃣ Web control integration:"
if [ -f "/root/hitchecker/bot/bot.py" ]; then
    echo "Checking bot.py for web hooks..."
    grep -n "webhook\|api_server\|flask\|aiohttp\|route" /root/hitchecker/bot/bot.py 2>/dev/null | head -10
fi
echo ""

# 13. Test bot token
echo "1️⃣3️⃣ Testing bot token:"
BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
BOT_INFO=$(curl -s --connect-timeout 5 "https://api.telegram.org/bot$BOT_TOKEN/getMe")
if echo "$BOT_INFO" | grep -q '"ok":true'; then
    BOT_USER=$(echo "$BOT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null)
    echo "✅ Bot: @$BOT_USER"
    echo "✅ Token is valid"
else
    echo "⚠️ Token issue: $BOT_INFO"
fi
echo ""

# 14. Check registered commands
echo "1️⃣4️⃣ Registered bot commands:"
GET_CMDS=$(curl -s --connect-timeout 5 "https://api.telegram.org/bot$BOT_TOKEN/getMyCommands")
if echo "$GET_CMDS" | grep -q '"ok":true'; then
    echo "Commands:"
    echo "$GET_CMDS" | python3 -c "import sys,json; cmds=json.load(sys.stdin).get('result',[]); [print(f'  /{c[\"command\"]} - {c[\"description\"]}') for c in cmds]" 2>/dev/null || echo "$GET_CMDS"
else
    echo "No commands registered or error: $GET_CMDS"
fi
echo ""

# 15. Check webhook
echo "1️⃣5️⃣ Telegram webhook:"
WEBHOOK=$(curl -s --connect-timeout 5 "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo")
WEBHOOK_URL=$(echo "$WEBHOOK" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('result',{}).get('url','Not set'))" 2>/dev/null)
echo "Webhook URL: $WEBHOOK_URL"
if echo "$WEBHOOK_URL" | grep -q "72.62.2.181"; then
    echo "✅ Webhook points to this server"
else
    echo "⚠️ Webhook may not be set correctly"
fi
echo ""

echo "=============================================="
echo "  DIAGNOSIS COMPLETE"
echo "=============================================="
echo ""
echo "📋 SUMMARY:"
echo "  Bot Location: $BOT_FILES"
echo "  Shopify API: $SHOP_FILES"
echo "  Web Server: $WEB_FILES"
echo "  Bot: @$BOT_USER"
echo "  Status: $(pgrep -f 'python.*bot.py' > /dev/null && echo 'RUNNING' || echo 'STOPPED')"
echo ""
echo "🔧 NEXT STEPS:"
echo "  1. If bot stopped: Use web interface to start"
echo "  2. If /start not working: Run FIX_ONLY_COMMANDS.sh"
echo "  3. Check Shopify API: Look at files in $SHOP_FILES"
echo ""
echo "💡 Web Interface:"
echo "  http://72.62.2.181:5000/"
echo "  (Controls bot start/stop)"
echo ""
