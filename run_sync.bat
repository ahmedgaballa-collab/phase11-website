@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
echo ==== %date% %time% ==== >> sync.log
python scraper.py --out status.json >> sync.log 2>&1
if %errorlevel%==0 python -c "import scraper,telegram_notify;scraper.load_dotenv();telegram_notify.main()" >> sync.log 2>&1
