#!/bin/bash

# Quick VPS Setup Script for Telegram Bot
# Run this script on your VPS to setup everything automatically

set -e

BOT_DIR="/opt/telegram-bot"
echo "=== Telegram Bot VPS Setup ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use: sudo $0)"
    exit 1
fi

# Update system
echo "[1/6] Updating system packages..."
apt update && apt upgrade -y

# Install Python
echo "[2/6] Installing Python..."
apt install -y python3 python3-pip

# Create bot directory
echo "[3/6] Creating bot directory..."
mkdir -p "$BOT_DIR"
cd "$BOT_DIR"

# Install dependencies
echo "[4/6] Installing Python dependencies..."
if [ -f "requirements.txt" ]; then
    pip3 install -r requirements.txt
else
    pip3 install telethon aiohttp aiofiles requests
fi

# Make scripts executable
echo "[5/6] Setting up scripts..."
chmod +x start.sh 2>/dev/null || true

# Setup systemd service
echo "[6/6] Setting up systemd service..."
if [ -f "telegram-bot.service" ]; then
    cp telegram-bot.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable telegram-bot
    echo "Service installed and enabled!"
else
    echo "Service file not found, skipping systemd setup."
fi

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Next steps:"
echo "1. Upload your config.json to $BOT_DIR"
echo "2. Upload all bot files to $BOT_DIR"
echo "3. Start the bot: systemctl start telegram-bot"
echo "4. Check status: systemctl status telegram-bot"
echo "5. View logs: journalctl -u telegram-bot -f"
echo ""
echo "Or run manually: cd $BOT_DIR && python3 bot.py"
