# 🚀 VPS Deployment Guide - Telegram Bot

## 📋 Quick Reference

**VPS Connection Details:**
- **Host/IP:** `72.62.2.181`
- **Username:** `root`
- **Password:** `,MIMIhapa123@#`
- **Target Directory:** `/opt/telegram-bot`

---

## 🎯 Method 1: WinSCP + PuTTY (Easiest - Recommended for Windows)

### Step 1: Download Required Tools

1. **WinSCP** (for file upload): https://winscp.net/eng/download.php
2. **PuTTY** (for SSH terminal): https://www.putty.org/

### Step 2: Upload Files with WinSCP

1. Open WinSCP
2. Enter connection details:
   ```
   File protocol: SFTP
   Host name: 72.62.2.181
   Port number: 22
   Username: root
   Password: ,MIMIhapa123@#
   ```
3. Click **Login**
4. Accept host key if prompted (click "Yes")
5. You'll see split screen:
   - **Left side:** Your local files
   - **Right side:** VPS files
6. Navigate on the **right side** to `/opt/` 
7. Create folder `telegram-bot` if it doesn't exist (right-click → New → Directory)
8. On the **left side**, navigate to this bot folder
9. **Select all files** (Ctrl+A)
10. **Drag** them to the right side (`/opt/telegram-bot/`)
11. Wait for upload to complete

### Step 3: Connect via PuTTY

1. Open PuTTY
2. Enter:
   ```
   Host Name (or IP address): 72.62.2.181
   Port: 22
   Connection type: SSH
   ```
3. Click **Open**
4. Login as: `root`
5. Password: `,MIMIhapa123@#` (won't display as you type - this is normal)

### Step 4: Install and Setup

Run these commands one by one:

```bash
# Navigate to bot directory
cd /opt/telegram-bot

# Make setup script executable
chmod +x setup_vps.sh

# Run automated setup
./setup_vps.sh

# Start the bot
systemctl start telegram-bot

# Check if it's running
systemctl status telegram-bot
```

### Step 5: Verify Bot is Working

```bash
# View live logs
journalctl -u telegram-bot -f
```

You should see messages like:
- "Bot started successfully"
- "Bot is now running..."

**Test in Telegram:**
1. Open Telegram
2. Search for your bot
3. Send `/start`
4. Bot should respond! ✅

---

## 🎯 Method 2: Windows PowerShell (No Extra Software)

Windows 10/11 has built-in SSH:

### Step 1: Open PowerShell as Administrator

Press `Win + X` → Select "Windows PowerShell (Admin)"

### Step 2: Connect via SSH

```powershell
ssh root@72.62.2.181
```

Type `yes` when prompted about host key
Enter password: `,MIMIhapa123@#`

### Step 3: Create Directory and Upload Files

```bash
# Create directory
mkdir -p /opt/telegram-bot
cd /opt/telegram-bot
```

**Now upload files using one of these methods:**

**Option A: Using pscp (PuTTY SCP)**
```powershell
# From PowerShell (new window)
pscp -r C:\Users\mrcyber\Downloads\Hitchk-Workflow` (1)\Hitchk-Workflow\hitchk-app\bot\* root@72.62.2.181:/opt/telegram-bot/
```

**Option B: Using WinSCP (easier)**
- Follow WinSCP instructions in Method 1

### Step 4: Install and Run

```bash
# Install dependencies
pip3 install -r requirements.txt

# Make scripts executable
chmod +x start.sh setup_vps.sh telegram-bot.service

# Setup systemd service
cp telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable telegram-bot

# Start bot
systemctl start telegram-bot

# Check status
systemctl status telegram-bot

# View logs
journalctl -u telegram-bot -f
```

---

## 🎯 Method 3: Direct Linux Commands (Advanced)

If you're comfortable with Linux:

```bash
# SSH to server
ssh root@72.62.2.181
# Password: ,MIMIhapa123@#

# Update system
apt update && apt upgrade -y

# Install Python
apt install -y python3 python3-pip

# Create directory
mkdir -p /opt/telegram-bot
cd /opt/telegram-bot

# Upload files (use WinSCP or pscp from another machine)

# Install dependencies
pip3 install -r requirements.txt

# Setup service
cp telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable telegram-bot
systemctl start telegram-bot

# Verify
systemctl status telegram-bot
journalctl -u telegram-bot -f
```

---

## 🔧 Troubleshooting

### Bot Won't Start

```bash
# Check if config.json exists
ls -la /opt/telegram-bot/config.json

# Test manually
cd /opt/telegram-bot
python3 bot.py

# Check for errors
journalctl -u telegram-bot -f
```

### Missing Dependencies

```bash
cd /opt/telegram-bot
pip3 install telethon aiohttp aiofiles requests
```

### Permission Issues

```bash
chmod 755 /opt/telegram-bot/*.sh
chmod 600 /opt/telegram-bot/config.json
chown -R root:root /opt/telegram-bot
```

### Check Python Version

```bash
python3 --version
# Should be 3.8 or higher
```

### Bot Crashes Immediately

Check logs:
```bash
journalctl -u telegram-bot --since today -f
```

Common issues:
- Missing config.json
- Wrong Python version
- Missing dependencies
- Port already in use

### Firewall Issues

```bash
# Check if firewall is blocking
ufw status

# Allow SSH (if needed)
ufw allow 22/tcp
ufw enable
```

---

## 📊 Monitoring & Maintenance

### Daily Checks

```bash
# Check bot status
systemctl status telegram-bot

# View recent logs
journalctl -u telegram-bot --since today

# Check disk space
df -h

# Check memory usage
free -h

# Check running processes
ps aux | grep bot.py
```

### Backup Important Files

```bash
# Create backup directory
mkdir -p /root/bot-backups/$(date +%Y-%m-%d)

# Backup JSON files
cp /opt/telegram-bot/*.json /root/bot-backups/$(date +%Y-%m-%d)/

# Download backups to local machine (optional)
# Use WinSCP to download /root/bot-backups/
```

### Update Bot

```bash
# Stop bot
systemctl stop telegram-bot

# Upload new files (using WinSCP)

# Restart bot
systemctl start telegram-bot

# Check logs
journalctl -u telegram-bot -f
```

---

## 🛡️ Security Recommendations

### 1. Change Root Password

```bash
passwd
```

### 2. Setup SSH Keys (More Secure than Password)

```bash
# Generate key on your local machine (PowerShell)
ssh-keygen -t rsa -b 4096

# Copy to VPS
ssh-copy-id root@72.62.2.181

# Test login (should not ask for password)
ssh root@72.62.2.181
```

### 3. Disable Password Authentication (After SSH Keys Work)

Edit `/etc/ssh/sshd_config`:
```bash
PasswordAuthentication no
PermitRootLogin prohibit-password
```

Then restart SSH:
```bash
systemctl restart sshd
```

### 4. Setup Firewall

```bash
# Install UFW
apt install -y ufw

# Allow SSH
ufw allow 22/tcp

# Enable firewall
ufw enable

# Check status
ufw status
```

### 5. Regular Updates

```bash
# Weekly updates
apt update && apt upgrade -y
```

---

## 📞 Quick Commands Reference

| Command | Description |
|---------|-------------|
| `systemctl start telegram-bot` | Start the bot |
| `systemctl stop telegram-bot` | Stop the bot |
| `systemctl restart telegram-bot` | Restart the bot |
| `systemctl status telegram-bot` | Check bot status |
| `journalctl -u telegram-bot -f` | View live logs |
| `systemctl enable telegram-bot` | Auto-start on boot |
| `systemctl disable telegram-bot` | Disable auto-start |
| `ps aux \| grep bot.py` | Check if bot process running |
| `cd /opt/telegram-bot` | Navigate to bot directory |

---

## ✅ Success Checklist

- [ ] Files uploaded to `/opt/telegram-bot/`
- [ ] Dependencies installed (`pip3 install -r requirements.txt`)
- [ ] Systemd service configured
- [ ] Bot started (`systemctl start telegram-bot`)
- [ ] Bot status shows "active (running)"
- [ ] Logs show "Bot started successfully"
- [ ] Bot responds to `/start` command in Telegram
- [ ] Auto-start enabled (`systemctl enable telegram-bot`)

---

## 🎉 You're Done!

Your Telegram bot is now running 24/7 on your VPS!

**Next Steps:**
1. Test all bot commands
2. Monitor logs for first few hours
3. Setup backups
4. Consider setting up SSH keys for better security

**Need Help?**
Check logs: `journalctl -u telegram-bot -f`

---

**Generated:** 2026-09-23
**Bot Version:** Telegram Checker Bot
**VPS IP:** 72.62.2.181
