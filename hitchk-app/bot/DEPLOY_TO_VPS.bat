@echo off
echo ===============================================
echo   Telegram Bot VPS Deployment Script
echo ===============================================
echo.
echo VPS Details:
echo   Host: 72.62.2.181
echo   User: root
echo   Password: ,MIMIhapa123@#
echo.
echo Steps to deploy:
echo.
echo 1. Install WinSCP from https://winscp.net
echo.
echo 2. Connect to VPS using WinSCP:
echo    - Host: 72.62.2.181
echo    - Username: root
echo    - Password: ,MIMIhapa123@#
echo    - Protocol: SFTP
echo.
echo 3. Upload entire bot folder to /opt/telegram-bot/
echo.
echo 4. Open PuTTY and connect to: 72.62.2.181
echo    Login: root
echo    Password: ,MIMIhapa123@#
echo.
echo 5. Run these commands in PuTTY:
echo.
echo    cd /opt/telegram-bot
echo    chmod +x setup_vps.sh
echo    ./setup_vps.sh
echo    systemctl start telegram-bot
echo    systemctl status telegram-bot
echo.
echo ===============================================
pause
