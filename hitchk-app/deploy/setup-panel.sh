#!/bin/bash
# The web panel on a fresh machine: node, the packages, the database,
# the build and the service.  It is safe to run again.
#
#   bash deploy/setup-panel.sh
#
set -e
APP=/root/hitchecker/hitchk-app

echo '=== node and the package manager: ==='
if ! command -v node >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash
  apt-get install -y -qq nodejs
fi
echo "   the node: $(node -v) | the npm: $(npm -v)"

echo '=== the database: ==='
if ! command -v psql >/dev/null 2>&1; then
  apt-get install -y -qq postgresql postgresql-contrib
fi
service postgresql start >/dev/null 2>&1 || true
sleep 3
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='checker'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "psql -c \"CREATE USER checker WITH PASSWORD 'checkerpass';\"" 2>&1 | tail -1
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='checker'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "psql -c 'CREATE DATABASE checker OWNER checker;'" 2>&1 | tail -1

echo '=== the packages: ==='
cd "$APP"
export HOME=/root
export NPM_CONFIG_CACHE=/root/.npm
if ! command -v pnpm >/dev/null 2>&1; then
  corepack enable >/dev/null 2>&1 || true
  npm install -g pnpm@9 >/dev/null 2>&1 || true
fi
if [ ! -d node_modules ] || [ ! -d node_modules/.bin ]; then
  pnpm install --frozen-lockfile 2>&1 | tail -3
fi
echo "   the packages: $(ls node_modules | wc -l)"

echo '=== the environment: ==='
cat > "$APP/.env" <<'EOF'
NODE_ENV=production
PORT=8080
DATABASE_URL=postgresql://checker:checkerpass@127.0.0.1:5432/checker
BOT_DATA_DIR=/root/hitchecker
SESSION_SECRET=mrcyber_tz_secret_key_2026_permanent
EOF
chmod 600 "$APP/.env"

echo '=== the tables: ==='
npx drizzle-kit push --force 2>&1 | tail -3

echo '=== the client, built: ==='
timeout 900 npx tsx script/build.ts 2>&1 | tail -3
echo "   the files: $(ls dist/public 2>/dev/null | wc -l)"

echo '=== the service: ==='
cat > /etc/systemd/system/web-panel.service <<'EOF'
[Unit]
Description=mrcyber-tz Web Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hitchecker/hitchk-app
Environment=NODE_ENV=production
Environment=PORT=8080
Environment=HOME=/root
Environment=DATABASE_URL=postgresql://checker:checkerpass@127.0.0.1:5432/checker
# The bot and the panel must read the same files, or a code made in
# one is unknown in the other.
Environment=BOT_DATA_DIR=/root/hitchecker
Environment=SESSION_SECRET=mrcyber_tz_secret_key_2026_permanent
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/bin/npx tsx server/index.ts
Restart=always
RestartSec=5
StandardOutput=append:/var/log/web-panel.log
StandardError=append:/var/log/web-panel.log

[Install]
WantedBy=multi-user.target
EOF
chmod 644 /etc/systemd/system/web-panel.service
systemctl daemon-reload
systemctl enable web-panel >/dev/null 2>&1 || true
systemctl restart web-panel
sleep 12
echo "   the panel: $(systemctl is-active web-panel)"
curl -s -o /dev/null -w '   the page: %{http_code}\n' -m 10 http://localhost:8080/ 2>/dev/null
