# Telegram Bot Startup Script
Write-Host "Starting Telegram Bot..." -ForegroundColor Green

# Change to bot directory
$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $botDir

# Check if config.json exists
if (-not (Test-Path "config.json")) {
    Write-Host "ERROR: config.json not found!" -ForegroundColor Red
    Write-Host "Please create config.json with your Telegram credentials." -ForegroundColor Yellow
    exit 1
}

# Check Python installation
try {
    $pythonCmd = Get-Command python -ErrorAction Stop
    Write-Host "Python found: $($pythonCmd.Source)" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Python not found!" -ForegroundColor Red
    exit 1
}

# Install required dependencies if missing
Write-Host "Checking dependencies..." -ForegroundColor Cyan
$requiredModules = @("telethon", "aiohttp", "aiofiles", "requests")
foreach ($module in $requiredModules) {
    try {
        Import-Module $module -ErrorAction Stop
        Write-Host "  ✓ $module installed" -ForegroundColor Green
    } catch {
        Write-Host "  Installing $module..." -ForegroundColor Yellow
        pip install $module --quiet
    }
}

# Start the bot
Write-Host "`nStarting bot..." -ForegroundColor Green
python bot.py
