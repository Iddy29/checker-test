@echo off
echo ===============================================
echo   Connecting to VPS (72.62.2.181)...
echo ===============================================
echo.
echo Opening PuTTY...
echo.
echo Login credentials:
echo   Username: root
echo   Password: ,MIMIhapa123@#
echo.
echo After login, run these commands:
echo   cd /opt/telegram-bot
echo   chmod +x setup_vps.sh
echo   ./setup_vps.sh
echo   systemctl start telegram-bot
echo   systemctl status telegram-bot
echo.
echo ===============================================
echo.

REM Try to open PuTTY if installed
if exist "C:\Program Files\PuTTY\putty.exe" (
    start "" "C:\Program Files\PuTTY\putty.exe" root@72.62.2.181
) else if exist "putty.exe" (
    start "" putty.exe root@72.62.2.181
) else (
    echo PuTTY not found! Download from: https://www.putty.org/
    echo.
    echo Or use Windows PowerShell:
    echo   ssh root@72.62.2.181
    echo.
    pause
    REM Try Windows built-in SSH
    ssh root@72.62.2.181
)
