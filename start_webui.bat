@echo off
chcp 65001 >nul
echo ============================================
echo  📈 股票投研自动化 — WebUI 管理界面
echo ============================================
echo.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -X utf8 -m uvicorn webui.main:app --host 0.0.0.0 --port 8080
pause
