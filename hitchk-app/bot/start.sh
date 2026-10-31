#!/bin/bash

# Telegram Bot Startup Script for VPS
echo "Starting Telegram Bot..."

# Change to bot directory
cd "$(dirname "$0")"

# Check if config.json exists
if [ ! -f "config.json" ]; then
    echo "ERROR: config.json not found!"
    echo "Please create config.json with your Telegram credentials."
    exit 1
fi

# Check Python installation
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found!"
    exit 1
fi

echo "Python found: $(which python3)"

# Install required dependencies
echo "Checking dependencies..."
pip3 install -r requirements.txt --quiet

# Start the bot
echo "Starting bot..."
python3 bot.py
