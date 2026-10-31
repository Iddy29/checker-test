@echo off
echo ==============================================
echo   Connecting to VPS: 72.62.2.181:22
echo   User: root
echo ==============================================
echo.

REM Check if Plink (PuTTY) is available
set PLINK_PATH=C:\Program Files\PuTTY\plink.exe
if not exist "%PLINK_PATH%" (
    echo ERROR: PuTTY not found!
    echo Download from: https://www.putty.org/
    echo.
    echo Or use manual SSH:
    echo   ssh root@72.62.2.181
    echo   Password: ,MIMIhapa123@#
    echo.
    pause
    goto MANUAL
)

echo Starting connection...
echo.

REM Create temporary script to upload
set TEMP_SCRIPT=%TEMP%\fix_bot.sh
(
echo #!/bin/bash
echo echo "Stopping old processes..."
echo pkill -9 -f python
echo sleep 2
echo.
echo echo "Updating config..."
echo cat ^> /root/hitchecker/bot/config.json ^<^< 'EOFCONFIG'
echo {
echo   "TELEGRAM_BOT_TOKEN": "8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo",
echo   "TELEGRAM_API_ID": "20464",
echo   "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a",
echo   "TELEGRAM_ADMIN_ID": "1517013110,8726037072,1834450801,8268547077,1214399247",
echo   "RAILWAY_SHOPIFY_API": "http://localhost:5000"
echo }
echo EOFCONFIG
echo.
echo echo "Installing dependencies..."
echo pip3 install aiofiles aiohttp telethon requests flask --quiet --upgrade
echo.
echo echo "Cleaning session..."
echo rm -f /root/hitchecker/bot/cc_bot.session*
echo.
echo echo "Registering bot commands..."
echo BOT_TOKEN="8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo"
echo curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyCommands" -H "Content-Type: application/json" -d '{"commands":[{"command":"start","description":"Start bot"},{"command":"help","description":"Help"},{"command":"cmds","description":"Commands"},{"command":"info","description":"Info"}]}' ^> /dev/null
echo curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/setMyName" -H "Content-Type: application/json" -d '{"name":"I-NETTZ Checker"}' ^> /dev/null
echo curl -s "https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=http://72.62.2.181:5000/webhook" ^> /dev/null
echo.
echo echo "Starting web server..."
echo cd /root/hitchecker
echo if [ -f "api_server.py" ]; then
echo     nohup python3 api_server.py ^> web.log 2^>^&1 ^&
echo elif [ -f "app.py" ]; then
echo     nohup python3 app.py ^> web.log 2^>^&1 ^&
echo fi
echo sleep 3
echo.
echo echo "Starting bot..."
echo cd /root/hitchecker/bot
echo nohup python3 bot.py ^> bot.log 2^>^&1 ^&
echo sleep 5
echo.
echo echo "=============================================="
echo echo "STATUS:"
echo ps aux ^| grep -v grep ^| grep python
echo echo ""
echo echo "Port 5000:"
echo netstat -tlnp 2^>/dev/null ^| grep 5000 ^|^| ss -tlnp 2^>/dev/null ^| grep 5000
echo echo ""
echo echo "Bot logs:"
echo tail -20 /root/hitchecker/bot/bot.log ^| grep -E "^(started|error)" ^|^| tail -20 /root/hitchecker/bot/bot.log
echo echo ""
echo echo "DONE! Test: https://t.me/Myhitz_bot"
echo echo "Send /start - should see menu!"
) > "%TEMP_SCRIPT%"

echo Script created: %TEMP_SCRIPT%
echo.
echo Connecting via SSH...
echo.

REM Connect and execute
"%PLINK_PATH%" -ssh -P 22 -pw ",MIMIhapa123@#" -batch root@72.62.2.181 "bash -s" < "%TEMP_SCRIPT%"

echo.
echo ==============================================
echo Connection closed
echo ==============================================
echo.
echo If you see errors above, try manual connection:
echo   ssh root@72.62.2.181
echo   Password: ,MIMIhapa123@#
echo.
pause
exit /b

:MANUAL
echo.
echo Manual commands to paste after SSH connection:
echo.
echo cd /root/hitchecker
echo pkill -9 -f python
echo sleep 2
echo cat ^> bot/config.json ^<^< 'EOF'
echo {"TELEGRAM_BOT_TOKEN":"8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo","TELEGRAM_API_ID":"20464","TELEGRAM_API_HASH":"c4a32e97023c4d3e6c8f5a9b2d1e3f4a"}
echo EOF
echo rm -f bot/cc_bot.session*
echo curl -s -X POST "https://api.telegram.org/bot8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo/setMyCommands" -d '{"commands":[{"command":"start","description":"Start"}]}'
echo nohup python3 api_server.py ^> web.log 2^>^&1 ^&
echo cd bot ^&^& nohup python3 bot.py ^> bot.log 2^>^&1 ^&
echo ps aux ^| grep python
echo Test: https://t.me/Myhitz_bot
echo.
pause
