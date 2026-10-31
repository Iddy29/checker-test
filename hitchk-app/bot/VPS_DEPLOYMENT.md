# VPS Deployment Guide for Telegram Bot

## Prerequisites
- Ubuntu/Debian VPS (recommended)
- Root or sudo access
- Python 3.8+ installed

## Step 1: Upload Bot Files to VPS

```bash
# Connect to your VPS
ssh root@your-vps-ip

# Create bot directory
mkdir -p /opt/telegram-bot
cd /opt/telegram-bot

# Upload your bot files (use SCP or SFTP)
# From your local machine:
# scp -r hitchk-app/bot/* root@your-vps-ip:/opt/telegram-bot/
```

## Step 2: Install Dependencies

```bash
# Update system packages
apt update && apt upgrade -y

# Install Python and pip
apt install -y python3 python3-pip

# Install required Python packages
pip3 install -r requirements.txt
```

## Step 3: Configure Environment (Optional)

If you prefer environment variables over config.json:

```bash
# Edit systemd service file
nano /etc/systemd/system/telegram-bot.service

# Add environment variables in [Service] section:
Environment="TELEGRAM_BOT_TOKEN=your_bot_token"
Environment="TELEGRAM_API_ID=your_api_id"
Environment="TELEGRAM_API_HASH=your_api_hash"
Environment="TELEGRAM_ADMIN_ID=your_admin_ids"
```

## Step 4: Setup Systemd Service (Recommended for Auto-Start)

```bash
# Copy service file
cp telegram-bot.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable service to start on boot
systemctl enable telegram-bot

# Start the bot
systemctl start telegram-bot

# Check status
systemctl status telegram-bot

# View logs
journalctl -u telegram-bot -f
```

## Step 5: Alternative - Run with Screen/Tmux

```bash
# Install screen
apt install -y screen

# Start bot in screen session
screen -S telegram-bot
./start.sh

# Detach: Ctrl+A, then D
# Reattach: screen -r telegram-bot
```

## Step 6: Setup Auto-Restart on Crash

Create a simple monitor script:

```bash
#!/bin/bash
# save as /opt/telegram-bot/monitor.sh

while true; do
    if ! pgrep -f "python3.*bot.py" > /dev/null; then
        echo "Bot crashed, restarting..." >> /var/log/bot-monitor.log
        cd /opt/telegram-bot
        python3 bot.py >> /var/log/bot.log 2>&1 &
    fi
    sleep 30
done
```

Run it:
```bash
chmod +x monitor.sh
nohup ./monitor.sh &
```

## Troubleshooting

### Bot won't start
```bash
# Check Python version
python3 --version

# Check if config.json exists
ls -la config.json

# Test import
python3 -c "import bot"
```

### Check logs
```bash
# Systemd logs
journalctl -u telegram-bot -f

# Bot logs
tail -f /var/log/bot.log
```

### Permission issues
```bash
# Make scripts executable
chmod +x start.sh monitor.sh

# Fix ownership
chown -R root:root /opt/telegram-bot
```

## Security Recommendations

1. **Protect config.json**: `chmod 600 config.json`
2. **Use firewall**: Allow only necessary ports
3. **Regular updates**: Keep system and packages updated
4. **Backup**: Regularly backup config.json and user data files
5. **Monitor**: Set up log monitoring for suspicious activity

## Quick Commands Reference

```bash
# Start bot
systemctl start telegram-bot

# Stop bot
systemctl stop telegram-bot

# Restart bot
systemctl restart telegram-bot

# Check status
systemctl status telegram-bot

# View logs
journalctl -u telegram-bot -f

# Enable auto-start
systemctl enable telegram-bot

# Disable auto-start
systemctl disable telegram-bot
```

## Testing Bot Connection

```bash
# Test if bot is running
curl -s https://api.telegram.org/botYOUR_BOT_TOKEN/getMe

# Should return bot information
```
