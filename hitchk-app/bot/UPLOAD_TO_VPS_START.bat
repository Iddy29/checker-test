@echo off
echo ============================================
echo   VPS Bot Fix - Simple Method
echo ============================================
echo.
echo This will upload fix script to your VPS
echo using WinSCP, then you can run it there.
echo.
pause

REM Check if WinSCP is installed
set WINSCP_PATH=C:\Program Files (x86)\WinSCP\WinSCP.com
if not exist "%WINSCP_PATH%" set WINSCP_PATH=C:\Program Files\WinSCP\WinSCP.com
if not exist "%WINSCP_PATH%" (
    echo ERROR: WinSCP not found!
    echo Please install from: https://winscp.net
    echo.
    echo OR manually:
    echo 1. Open WinSCP
    echo 2. Connect to 72.62.2.181
    echo    User: root
    echo    Pass: MIMIhapa123@#
    echo 3. Upload fix_vps.sh to /root/
    echo 4. SSH to VPS and run it
    echo.
    pause
    exit /b
)

echo Creating fix script...
(
echo #!/bin/bash
echo cd /root/hitchecker
echo pkill -9 -f python
echo sleep 2
echo.
echo echo "Updating config..."
echo cat ^> bot/config.json ^<^< 'EOFCONFIG'
echo {
echo   "TELEGRAM_BOT_TOKEN": "8589911426:AAFLChEyrmBhO4mSDnDwIrmjbvumTvSpIbo",
echo   "TELEGRAM_API_ID": "20464",
echo   "TELEGRAM_API_HASH": "c4a32e97023c4d3e6c8f5a9b2d1e3f4a"
echo }
echo EOFCONFIG
echo.
echo echo "Cleaning session..."
echo rm -f bot/cc_bot.session*
echo.
echo echo "Starting bot..."
echo nohup python3 bot.py ^> bot.log 2^>^&1 ^&
echo sleep 5
echo.
echo echo "Checking status..."
echo ps aux ^| grep -v grep ^| grep python
echo echo ""
echo echo "=========================================="
echo echo "DONE!"
echo echo "Test: https://t.me/Myhitz_bot"
echo echo "Send /start"
echo echo "=========================================="
) > "%TEMP%\fix_vps.sh"

echo Uploading to VPS...
"%WINSCP_PATH%" /command ^
    "open sftp://root:MIMIhapa123@#@72.62.2.181/ -hostkey=*" ^
    "put %TEMP%\fix_vps.sh /root/" ^
    "exit"

echo.
echo ============================================
echo Upload complete!
echo ============================================
echo.
echo NOW CONNECT VIA PuTTY:
echo 1. Open PuTTY
echo 2. Connect to: 72.62.2.181
echo 3. Login: root / MIMIhapa123@#
echo 4. Run: chmod +x /root/fix_vps.sh
echo 5. Run: /root/fix_vps.sh
echo.
echo The script will fix everything!
echo.
pause
