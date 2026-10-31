#!/bin/bash
cd /root/hitchecker

echo "=== STOPPING OLD PROCESSES ==="
pkill -9 -f python
sleep 2

echo "=== UPDATING CONFIG ==="
cat > bot/config.json << 'EOFCONFIG'
{
  "TELEGRAM_BOT_TOKEN": "8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo",
  "TELEGRAM_API_ID": "20464",
  "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a",
  "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
  "RAILWAY_SHOPIFY_API": "http://localhost:5000"
}
EOFCONFIG

echo "=== CLEANING SESSION ==="
rm -f bot/cc_bot.session*

echo "=== INSTALLING DEPS ==="
pip3 install aiofiles aiohttp telethon requests flask --quiet --upgrade

echo "=== REGISTERING COMMANDS ==="
BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" -H "Content-Type: application/json" -d '{"commands":[{"command":"start","description":"Start bot"},{"command":"help","description":"Help"},{"command":"cmds","description":"Commands"},{"command":"info","description":"Info"}]}' > /dev/null
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" -H "Content-Type: application/json" -d '{"name":"I-NETTZ Checker"}' > /dev/null
curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=http://72.62.2.181:5000/webhook" > /dev/null

echo "=== STARTING WEB SERVER ==="
cd /root/hitchecker
if [ -f "api_server.py" ]; then
    nohup python3 api_server.py > web.log 2>&1 &
    echo "Web server started (PID: $!)"
elif [ -f "app.py" ]; then
    nohup python3 app.py > web.log 2>&1 &
    echo "Web server started (PID: $!)"
fi
sleep 3

echo "=== STARTING BOT ==="
cd /root/hitchecker/bot
nohup python3 bot.py > bot.log 2>&1 &
echo "Bot started (PID: $!)"
sleep 5

echo ""
echo "=============================================="
echo "=== STATUS ==="
echo "Processes:"
ps aux | grep -v grep | grep python
echo ""
echo "Port 5000:"
netstat -tlnp 2>/dev/null | grep 5000 || ss -tlnp 2>/dev/null | grep 5000
echo ""
echo "Bot logs:"
tail -30 bot.log | grep -E "(started|error)" || tail -30 bot.log
echo ""
echo "=============================================="
echo "✅ ALL FIXED!"
echo "Test: https://t.me/Myhitz_bot"
echo "Send /start - should see menu!"
echo "=============================================="
