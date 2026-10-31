#!/bin/bash

echo "=============================================="
echo "  DIAGNOSING VPS Bot Setup"
echo "  Finding: Bot controller & Web interface"
echo "=============================================="
echo ""

# 1. Find all Python processes
echo "1️⃣ Running Python processes:"
ps aux | grep -v grep | grep python || echo "  No Python processes"
echo ""

# 2. Find bot.py locations
echo "2️⃣ Bot files found:"
find /root /opt /home -name "bot.py" 2>/dev/null | grep -v __pycache__ || echo "  None found"
echo ""

# 3. Find web server files
echo "3️⃣ Web server files:"
find /root /opt /home -name "api_server.py" -o -name "app.py" -o -name "server.py" -o -name "main.py" 2>/dev/null | grep -v __pycache__ | head -10 || echo "  None found"
echo ""

# 4. Check what's on port 5000
echo "4️⃣ Port 5000 status:"
netstat -tlnp 2>/dev/null | grep 5000 || ss -tlnp 2>/dev/null | grep 5000 || echo "  Nothing on port 5000"
echo ""

# 5. Find hitchecker directory
echo "5️⃣ HitChecker directory structure:"
if [ -d "/root/hitchecker" ]; then
    ls -la /root/hitchecker/ 2>/dev/null | head -20
    echo ""
    echo "Subdirectories:"
    find /root/hitchecker -maxdepth 2 -type d 2>/dev/null | head -20
else
    echo "  /root/hitchecker not found"
fi
echo ""

# 6. Check for process managers
echo "6️⃣ Process managers (systemd/supervisor):"
systemctl list-units 2>/dev/null | grep -E "(bot|telegram|checker)" | head -10 || echo "  No systemd units"
ls -la /etc/supervisor/conf.d/ 2>/dev/null || echo "  No supervisor"
echo ""

# 7. Find config files
echo "7️⃣ Config files:"
find /root/hitchecker -name "config.json" -o -name ".env" -o -name "settings.json" 2>/dev/null | head -10
echo ""

# 8. Check bot config if exists
if [ -f "/root/hitchecker/bot/config.json" ]; then
    echo "8️⃣ Bot config.json:"
    cat /root/hitchecker/bot/config.json 2>/dev/null
    echo ""
fi

# 9. Check for web control scripts
echo "9️⃣ Looking for web control scripts:"
find /root/hitchecker -name "*control*" -o -name "*manager*" -o -name "*admin*" 2>/dev/null | head -10
echo ""

# 10. Check recent logs
echo "🔟 Recent logs:"
find /root/hitchecker -name "*.log" 2>/dev/null | head -5 | while read logfile; do
    echo "File: $logfile"
    tail -10 "$logfile" 2>/dev/null | head -5
    echo "---"
done
echo ""

# 11. Check if bot.py has web hooks
if [ -f "/root/hitchecker/bot/bot.py" ]; then
    echo "1️⃣1️⃣ Bot.py web integration:"
    grep -n "webhook\|api\|server\|flask\|aiohttp" /root/hitchecker/bot/bot.py 2>/dev/null | head -10
    echo ""
fi

# 12. Check for startup scripts
echo "1️⃣2️⃣ Startup scripts:"
ls -la /root/hitchecker/*.sh 2>/dev/null || echo "  No .sh files in hitchecker root"
ls -la /root/hitchecker/bot/*.sh 2>/dev/null || echo "  No .sh files in bot directory"
echo ""

echo "=============================================="
echo "  DIAGNOSIS COMPLETE"
echo "=============================================="
echo ""
echo "📋 SUMMARY:"
echo "  - Bot location: /root/hitchecker/bot/"
echo "  - Web interface: Port 5000"
echo "  - Bot is controlled by web interface"
echo ""
echo "🔍 NEXT STEP:"
echo "  The bot was working before with minimal config."
echo "  The web interface controls start/stop."
echo ""
echo "  To fix /start command WITHOUT breaking web control:"
echo "  1. Keep existing config minimal"
echo "  2. Just register commands with Telegram"
echo "  3. Don't change bot.py logic"
echo ""
echo "  Run: ./FIX_ONLY_COMMANDS.sh"
echo ""
