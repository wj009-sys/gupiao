@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent7 START >> logs\scheduler.log

:: 综合决策（含质量审核+打回重做+冲突仲裁）
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent7-决策\leader.py
echo [%date% %time%] Agent7 DONE >> logs\scheduler.log
