@echo off
REM ============================================================================
REM  Deploy the /an fix to the VPS
REM    bot/gates/authnet.py       - the rewritten gate
REM    bot/bot_settings.json      - enables the 'an' gateway
REM    bot/authnet_proxies.json   - proxies that solve the captcha
REM ============================================================================
setlocal

set VPS_HOST=72.62.2.181
set VPS_USER=root
set VPS_PASS=,MIMIhapa123@#
set BOT_DIR=/root/hitchecker/bot

set PLINK="C:\Program Files\PuTTY\plink.exe"
set PSCP="C:\Program Files\PuTTY\pscp.exe"

set LOCAL_DIR=%~dp0

echo ===============================================
echo   Deploying /an fix to %VPS_HOST%
echo ===============================================
echo.

echo [1/4] Checking SSH reachability...
"%PLINK%" -batch -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "echo OK" >nul 2>&1
if errorlevel 1 (
  echo   FAILED - VPS not reachable on port 22.
  echo   Nothing was changed. Check the server panel.
  exit /b 1
)
echo   VPS reachable.
echo.

echo [2/4] Backing up current files...
"%PLINK%" -batch -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "cp %BOT_DIR%/gates/authnet.py %BOT_DIR%/gates/authnet.py.bak && cp %BOT_DIR%/bot_settings.json %BOT_DIR%/bot_settings.json.bak" 2>&1 | findstr /v "^$"
echo   Backups written to *.bak
echo.

echo [3/4] Uploading files...
"%PSCP%" -batch -pw "%VPS_PASS%" "%LOCAL_DIR%gates\authnet.py"          %VPS_USER%@%VPS_HOST%:%BOT_DIR%/gates/authnet.py
if errorlevel 1 goto :uploadfail
"%PSCP%" -batch -pw "%VPS_PASS%" "%LOCAL_DIR%bot_settings.json"         %VPS_USER%@%VPS_HOST%:%BOT_DIR%/bot_settings.json
if errorlevel 1 goto :uploadfail
"%PSCP%" -batch -pw "%VPS_PASS%" "%LOCAL_DIR%authnet_proxies.json"      %VPS_USER%@%VPS_HOST%:%BOT_DIR%/authnet_proxies.json
if errorlevel 1 goto :uploadfail
echo   Files uploaded.
echo.

echo [4/4] Restarting the bot and verifying...
"%PLINK%" -batch -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "cd %BOT_DIR% && systemctl restart hitchecker-bot 2>/dev/null || systemctl restart telegram-bot 2>/dev/null; sleep 6; systemctl is-active hitchecker-bot telegram-bot 2>/dev/null; ls -la %BOT_DIR%/gates/authnet.py %BOT_DIR%/authnet_proxies.json" 2>&1
echo.

echo ===============================================
echo   Done. Test in Telegram:
echo     /an 4111111111111111^|12^|29^|123
echo   Expect: Charged - Transaction Approved
echo ===============================================
goto :eof

:uploadfail
echo   UPLOAD FAILED - nothing restarted, bot still running the old code.
exit /b 1
