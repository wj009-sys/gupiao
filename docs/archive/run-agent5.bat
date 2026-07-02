@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent5 START >> logs\scheduler.log

:: 多因子选股（默认晚间模式，可加 --mode pre_market/intraday/noon/evening）
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent5-选股\stock_picker.py --top-n 5 %*
echo [%date% %time%] Agent5 DONE >> logs\scheduler.log
