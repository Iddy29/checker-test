#!/bin/bash
set -e
echo "=== Shopify Bot Update ==="

# Find the bot directory
echo "Searching for bot.py..."
BOT_DIR=$(find / -name "bot.py" -path "*/bot/*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)

if [ -z "$BOT_DIR" ]; then
    echo "Could not find bot directory automatically."
    echo "Please enter the path to your bot folder (the one containing bot.py):"
    read -p "Path: " BOT_DIR
fi

echo "Bot directory: $BOT_DIR"
echo ""

# Backup
echo "Backing up..."
BACKUP="$BOT_DIR/../backup_$(date +%s)"
mkdir -p "$BACKUP"
cp "$BOT_DIR/bot.py" "$BACKUP/" 2>/dev/null || true
cp "$BOT_DIR/gateways.py" "$BACKUP/" 2>/dev/null || true
cp "$BOT_DIR/web_checker.py" "$BACKUP/" 2>/dev/null || true
cp "$BOT_DIR/web_group_log.py" "$BACKUP/" 2>/dev/null || true
cp "$BOT_DIR/web_forward_hit.py" "$BACKUP/" 2>/dev/null || true
echo "Backup saved to: $BACKUP"
echo ""

# Download from GitHub
echo "Downloading updated files..."
REPO="https://raw.githubusercontent.com/Iddy29/checker-test/master"

cd "$BOT_DIR"
curl -sSL -o bot.py "$REPO/hitchk-app/bot/bot.py"
curl -sSL -o gateways.py "$REPO/hitchk-app/bot/gateways.py"
curl -sSL -o web_checker.py "$REPO/hitchk-app/bot/web_checker.py"
curl -sSL -o web_group_log.py "$REPO/hitchk-app/bot/web_group_log.py"
curl -sSL -o web_forward_hit.py "$REPO/hitchk-app/bot/web_forward_hit.py"
echo "Bot files updated."
echo ""

# Server files
PARENT_DIR=$(dirname "$BOT_DIR")
if [ -d "$PARENT_DIR/server" ]; then
    cd "$PARENT_DIR/server"
    curl -sSL -o botManager.ts "$REPO/hitchk-app/server/botManager.ts"
    curl -sSL -o routes.ts "$REPO/hitchk-app/server/routes.ts"
    echo "Server files updated."
fi
echo ""

# Client files
if [ -d "$PARENT_DIR/client/src/pages" ]; then
    cd "$PARENT_DIR/client/src/pages"
    curl -sSL -o checker.tsx "$REPO/hitchk-app/client/src/pages/checker.tsx"
    curl -sSL -o auto-shopify.tsx "$REPO/hitchk-app/client/src/pages/auto-shopify.tsx"
    echo "Client files updated."
fi
echo ""

# Restart bot
echo "=== Restarting bot ==="
if command -v pm2 &> /dev/null; then
    pm2 restart all 2>/dev/null && echo "Restarted via pm2." || echo "pm2 restart failed — restart manually."
elif systemctl list-units --type=service 2>/dev/null | grep -q bot; then
    systemctl restart bot 2>/dev/null && echo "Restarted via systemctl." || echo "systemctl restart failed."
else
    echo "Could not auto-detect how to restart."
    echo "Please restart your bot manually."
fi

echo ""
echo "=== DONE! Test /shp in Telegram ==="