@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo Installing Playwright browsers...
playwright install chromium
echo.
echo Setup complete! Now edit config.json with your credentials and run:
echo   python main.py --dry-run
echo   python main.py
pause
