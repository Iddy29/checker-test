@echo off
REM ============================================================================
REM  Deploy the /an fix to a VPS over SSH (plink + pscp)
REM
REM  Usage:
REM    DEPLOY_AN_FIX_V2.bat <host> <user> <password> [hostkey]
REM
REM  Example:
REM    DEPLOY_AN_FIX_V2.bat 91.124.130.211 root "rr*0;4Bq1A[3ZHa3" "ssh-ed25519 255 SHA256:C3Iv2..."
REM ============================================================================
setlocal enabledelayedexpansion

if "%~1"=="" goto :usage
if "%~2"=="" goto :usage
if "%~3"=="" goto :usage

set VPS_HOST=%~1
set VPS_USER=%~2
set VPS_PASS=%~3
set VPS_KEY=%~4
if "%VPS_KEY%"=="" set VPS_KEY=*  REM accept any host key (prompt if interactive)

set PLINK="C:\Program Files\PuTTY\plink.exe"
set PSCP="C:\Program Files\PuTTY\pscp.exe"
set LOCAL_DIR=%~dp0
set REMOTE_DIR=/root/hitchecker/hitchk-app

if "%VPS_KEY%"=="*" (
  set PLINKOPT=
  set PSCPOPT=
) else (
  set PLINKOPT=-hostkey "%VPS_KEY%"
  set PSCPOPT=-hostkey "%VPS_KEY%"
)

echo ===============================================
echo   Deploying /an fix to %VPS_HOST%
echo ===============================================
echo.

echo [1/5] Checking SSH reachability...
%PLINK% -batch %PLINKOPT% -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "echo OK" >nul 2>&1
if errorlevel 1 (
  echo   FAILED - cannot reach %VPS_HOST%
  exit /b 1
)
echo   Reachable.
echo.

echo [2/5] Backing up current files...
%PLINK% -batch %PLINKOPT% -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "cd %REMOTE_DIR% && cp bot/gates/authnet.py bot/gates/authnet.py.bak 2>/dev/null; cp bot/gates/authnet.py bot/gates/authnet.py.new 2>/dev/null; echo backed up"
echo.

echo [3/5] Uploading python files...
%PSCP% -batch %PSCPOPT% -pw "%VPS_PASS%" "%LOCAL_DIR%gates\authnet.py"      %VPS_USER%@%VPS_HOST%:%REMOTE_DIR%/bot/gates/authnet.py
if errorlevel 1 goto :uploadfail
%PSCP% -batch %PSCPOPT% -pw "%VPS_PASS%" "%LOCAL_DIR%web_authnet_check.py"  %VPS_USER%@%VPS_HOST%:%REMOTE_DIR%/bot/web_authnet_check.py
if errorlevel 1 goto :uploadfail
%PSCP% -batch %PSCPOPT% -pw "%VPS_PASS%" "%LOCAL_DIR%authnet_proxies.json"  %VPS_USER%@%VPS_HOST%:%REMOTE_DIR%/bot/authnet_proxies.json
if errorlevel 1 goto :uploadfail
%PSCP% -batch %PSCPOPT% -pw "%VPS_PASS%" "%LOCAL_DIR%bot_settings.json"     %VPS_USER%@%VPS_HOST%:%REMOTE_DIR%/bot/bot_settings.json
if errorlevel 1 goto :uploadfail
echo   Uploaded.
echo.

echo [4/5] Enabling the 'an' gateway and restarting the bot...
%PLINK% -batch %PLINKOPT% -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "cd %REMOTE_DIR% && python3 -c \"import json;p='bot/bot_settings.json';d=json.load(open(p));d.setdefault('gateway_settings',{}).setdefault('an',{})['enabled']=True;json.dump(d,open(p,'w'),indent=2);print('an enabled')\" && systemctl restart hitchecker-bot && sleep 5 && systemctl is-active hitchecker-bot"
echo.

echo [5/5] Verifying the gate on the VPS...
%PLINK% -batch %PLINKOPT% -pw "%VPS_PASS%" %VPS_USER%@%VPS_HOST% "cd %REMOTE_DIR%/bot && python3 -c \"import sys;sys.path.insert(0,'.');from gates import authnet;print('import OK, sites =',len(authnet.AUTHNET_SITES))\""
echo.

echo ===============================================
echo   Done. Test in Telegram:  /an 4111111111111111^|12^|29^|123
echo ===============================================
goto :eof

:uploadfail
echo   UPLOAD FAILED - bot was NOT restarted.
exit /b 1

:usage
echo Usage: %~nx0 ^<host^> ^<user^> ^<password^> [hostkey]
exit /b 1
