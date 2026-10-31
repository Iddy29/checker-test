# Deploy Fix to VPS and Run It
# This script uploads the fix and runs it automatically

$VPS_HOST = "72.62.2.181"
$VPS_USER = "root"
$VPS_PASSWORD = ",MIMIhapa123@#"

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Deploying Fix to VPS ($VPS_HOST)" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# Check if running from bot directory
if (-not (Test-Path "bot.py")) {
    Write-Host "ERROR: Run this script from the bot directory!" -ForegroundColor Red
    Write-Host "Current directory: $(Get-Location)" -ForegroundColor Yellow
    exit 1
}

Write-Host "✓ Running from bot directory" -ForegroundColor Green
Write-Host ""

# Create secure credential
$securePassword = ConvertTo-SecureString $VPS_PASSWORD -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($VPS_USER, $securePassword)

# Step 1: Test connection
Write-Host "Step 1: Testing SSH connection..." -ForegroundColor Cyan
try {
    # Note: This requires PSSH module or plink.exe
    # Alternative: Use pscp/pscp from PuTTY
    
    $testCmd = "echo 'Connection test'"
    
    # Try with plink (PuTTY command line)
    $plinkPath = "C:\Program Files\PuTTY\plink.exe"
    if (Test-Path $plinkPath) {
        $result = & $plinkPath -ssh $VPS_USER@$VPS_HOST -pw $VPS_PASSWORD -batch $testCmd 2>&1
        if ($result -like "*Connection test*") {
            Write-Host "  ✓ SSH connection successful" -ForegroundColor Green
        } else {
            throw "Connection failed"
        }
    } else {
        Write-Host "  ⚠ PuTTY not found, trying alternative method..." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Please install PuTTY or use WinSCP to upload files manually." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Manual steps:" -ForegroundColor Cyan
        Write-Host "  1. Open WinSCP and connect to $VPS_HOST" -ForegroundColor White
        Write-Host "  2. Upload fix_bot_on_vps.sh to bot directory" -ForegroundColor White
        Write-Host "  3. Connect via SSH/PuTTY to $VPS_HOST" -ForegroundColor White
        Write-Host "  4. Run: chmod +x fix_bot_on_vps.sh && ./fix_bot_on_vps.sh" -ForegroundColor White
        Write-Host ""
        pause
        exit 1
    }
} catch {
    Write-Host "  ✗ Connection failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Step 2: Upload fix script
Write-Host ""
Write-Host "Step 2: Uploading fix script..." -ForegroundColor Cyan

$fixScript = Get-Content "fix_bot_on_vps.sh" -Raw -ErrorAction SilentlyContinue
if (-not $fixScript) {
    Write-Host "  ✗ fix_bot_on_vps.sh not found!" -ForegroundColor Red
    Write-Host "  Creating simple inline fix..." -ForegroundColor Yellow
    
    $fixScript = @'
#!/bin/bash
echo "=== Applying Quick Fix ==="
cd /opt/telegram-bot 2>/dev/null || cd ~/telegram-bot 2>/dev/null || cd ~

# Backup
cp bot.py bot.py.backup.$(date +%s) 2>/dev/null

# Install deps
pip3 install aiofiles aiohttp telethon requests --quiet --upgrade

# Fix config loading
python3 << 'PYFIX'
try:
    with open("bot.py", "r") as f:
        content = f.read()
    
    if "def _load_config():" not in content:
        old = 'API_ID = os.environ.get("TELEGRAM_API_ID", "")'
        new = '''def _load_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

_config = _load_config()

API_ID = os.environ.get("TELEGRAM_API_ID", _config.get("TELEGRAM_API_ID", ""))'''
        content = content.replace(old, new)
        content = content.replace('API_HASH = os.environ.get("TELEGRAM_API_HASH", "")', 'API_HASH = os.environ.get("TELEGRAM_API_HASH", _config.get("TELEGRAM_API_HASH", ""))')
        content = content.replace('BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")', 'BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _config.get("TELEGRAM_BOT_TOKEN", ""))')
        content = content.replace('ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", "").split(",") if x.strip()]', 'ADMIN_ID = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_ID", _config.get("TELEGRAM_ADMIN_ID", "")).split(",") if x.strip()]')
        content = content.replace('GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", "0"))', 'GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID", _config.get("TELEGRAM_GROUP_ID", "0")))')
        
        with open("bot.py", "w") as f:
            f.write(content)
        print("Fixed config loading")
    else:
        print("Config already fixed")
except Exception as e:
    print(f"Error: {e}")
PYFIX

# Test
python3 -c "import bot" && echo "✓ Bot imports OK" || echo "✗ Import failed"

# Restart
pkill -f "python.*bot.py" 2>/dev/null
nohup python3 bot.py > bot.log 2>&1 &
sleep 2
ps aux | grep -v grep | grep "python.*bot.py" && echo "✓ Bot running" || echo "✗ Bot not running"

echo "=== Fix Complete ==="
'@
}

# Upload using pscp if available
$pscpPath = "C:\Program Files\PuTTY\pscp.exe"
if (Test-Path $pscpPath) {
    Write-Host "  Uploading fix script..." -ForegroundColor Gray
    $fixScript | Out-File -FilePath "$env:TEMP\fix_bot_on_vps.sh" -Encoding ASCII -NoNewline
    & $pscpPath -pw $VPS_PASSWORD "$env:TEMP\fix_bot_on_vps.sh" "$($VPS_USER)@$($VPS_HOST):/opt/telegram-bot/" 2>&1
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ Script uploaded" -ForegroundColor Green
    } else {
        Write-Host "  ⚠ Upload may have failed" -ForegroundColor Yellow
    }
} else {
    Write-Host "  ⚠ pscp.exe not found" -ForegroundColor Yellow
    Write-Host "  Please upload fix_bot_on_vps.sh manually using WinSCP" -ForegroundColor Yellow
}

# Step 3: Run fix on VPS
Write-Host ""
Write-Host "Step 3: Running fix on VPS..." -ForegroundColor Cyan

if (Test-Path $plinkPath) {
    & $plinkPath -ssh $VPS_USER@$VPS_HOST -pw $VPS_PASSWORD -batch "chmod +x /opt/telegram-bot/fix_bot_on_vps.sh && /opt/telegram-bot/fix_bot_on_vps.sh" 2>&1 | ForEach-Object {
        Write-Host $_ -ForegroundColor Gray
    }
} else {
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "  MANUAL ACTION REQUIRED" -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Connect to your VPS and run:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  ssh root@$VPS_HOST" -ForegroundColor White
    Write-Host "  # Password: $VPS_PASSWORD" -ForegroundColor White
    Write-Host ""
    Write-Host "  cd /opt/telegram-bot" -ForegroundColor White
    Write-Host "  chmod +x fix_bot_on_vps.sh" -ForegroundColor White
    Write-Host "  ./fix_bot_on_vps.sh" -ForegroundColor White
    Write-Host ""
    Write-Host "Or run the quick fix commands in HOW_TO_FIX_VPS.txt" -ForegroundColor Yellow
    Write-Host ""
    pause
}

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Deployment Complete!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Check your VPS to verify the bot is running:" -ForegroundColor Yellow
Write-Host "  ssh root@$VPS_HOST" -ForegroundColor White
Write-Host "  systemctl status telegram-bot" -ForegroundColor White
Write-Host "  journalctl -u telegram-bot -f" -ForegroundColor White
Write-Host ""
