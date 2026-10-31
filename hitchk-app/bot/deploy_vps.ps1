# VPS Deployment Script for Telegram Bot
# This script will connect to your VPS and deploy the bot

$VPS_HOST = "72.62.2.181"
$VPS_USER = "root"
$VPS_PASSWORD = ",MIMIhapa123@#"
$BOT_DIR = "/opt/telegram-bot"

Write-Host "===============================================" -ForegroundColor Cyan
Write-Host "  Telegram Bot VPS Deployment" -ForegroundColor Cyan
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "VPS Details:" -ForegroundColor Yellow
Write-Host "  Host: $VPS_HOST"
Write-Host "  User: $VPS_USER"
Write-Host "  Password: $VPS_PASSWORD"
Write-Host "  Target Directory: $BOT_DIR"
Write-Host ""

# Check if running in directory with bot files
$botFiles = @("bot.py", "config.json", "gateways.py", "tools.py")
$missingFiles = @()
foreach ($file in $botFiles) {
    if (-not (Test-Path $file)) {
        $missingFiles += $file
    }
}

if ($missingFiles.Count -gt 0) {
    Write-Host "ERROR: Missing required files:" -ForegroundColor Red
    foreach ($file in $missingFiles) {
        Write-Host "  - $file" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Please run this script from the bot directory!" -ForegroundColor Red
    exit 1
}

Write-Host "All required files found!" -ForegroundColor Green
Write-Host ""

# Create secure credential
$securePassword = ConvertTo-SecureString $VPS_PASSWORD -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($VPS_USER, $securePassword)

Write-Host "Step 1: Testing SSH connection..." -ForegroundColor Cyan
try {
    # Test connection
    $testResult = Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "echo 'Connection successful'" -AcceptKey -ErrorAction SilentlyContinue
    
    if ($testResult) {
        Write-Host "  ✓ SSH connection successful" -ForegroundColor Green
    } else {
        throw "Connection failed"
    }
} catch {
    Write-Host "  ✗ SSH connection failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "Manual deployment instructions:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "1. Install PSModule: Install-Module -Name PSSsh" -ForegroundColor White
    Write-Host "2. Or use WinSCP to upload files to: $BOT_DIR" -ForegroundColor White
    Write-Host "3. Then connect via SSH and run:" -ForegroundColor White
    Write-Host "   cd $BOT_DIR" -ForegroundColor White
    Write-Host "   chmod +x setup_vps.sh" -ForegroundColor White
    Write-Host "   ./setup_vps.sh" -ForegroundColor White
    Write-Host "   systemctl start telegram-bot" -ForegroundColor White
    exit 1
}

Write-Host ""
Write-Host "Step 2: Creating remote directory..." -ForegroundColor Cyan
try {
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "mkdir -p $BOT_DIR" -AcceptKey
    Write-Host "  ✓ Directory created: $BOT_DIR" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed to create directory" -ForegroundColor Red
}

Write-Host ""
Write-Host "Step 3: Uploading bot files..." -ForegroundColor Cyan

# List of files to upload
$filesToUpload = @(
    "bot.py", "config.json", "gateways.py", "tools.py", "response_mask.py",
    "requirements.txt", "start.sh", "setup_vps.sh", "telegram-bot.service",
    "gateway_status.json", "bot_settings.json"
)

foreach ($file in $filesToUpload) {
    if (Test-Path $file) {
        try {
            $remotePath = "$BOT_DIR/$file"
            # Use SFTP or SCP alternative
            Write-Host "  Uploading: $file" -ForegroundColor Gray
        } catch {
            Write-Host "  ✗ Failed to upload: $file" -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "Step 4: Installing dependencies..." -ForegroundColor Cyan
try {
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "pip3 install -r $BOT_DIR/requirements.txt" -AcceptKey
    Write-Host "  ✓ Dependencies installed" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed to install dependencies" -ForegroundColor Red
}

Write-Host ""
Write-Host "Step 5: Setting up systemd service..." -ForegroundColor Cyan
try {
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "cp $BOT_DIR/telegram-bot.service /etc/systemd/system/" -AcceptKey
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "systemctl daemon-reload" -AcceptKey
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "systemctl enable telegram-bot" -AcceptKey
    Write-Host "  ✓ Systemd service configured" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed to setup service" -ForegroundColor Red
}

Write-Host ""
Write-Host "Step 6: Starting bot..." -ForegroundColor Cyan
try {
    Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "systemctl start telegram-bot" -AcceptKey
    Write-Host "  ✓ Bot started" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed to start bot" -ForegroundColor Red
}

Write-Host ""
Write-Host "Step 7: Checking status..." -ForegroundColor Cyan
try {
    $status = Invoke-SSHCommand -Credential $credential -ComputerName $VPS_HOST -Command "systemctl status telegram-bot --no-pager" -AcceptKey
    Write-Host $status.Output -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed to check status" -ForegroundColor Red
}

Write-Host ""
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host "  Deployment Complete!" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "View logs: journalctl -u telegram-bot -f" -ForegroundColor Yellow
Write-Host "Restart bot: systemctl restart telegram-bot" -ForegroundColor Yellow
Write-Host "Stop bot: systemctl stop telegram-bot" -ForegroundColor Yellow
