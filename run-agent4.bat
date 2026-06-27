@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent4 START >> logs\scheduler.log
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent4-复盘\review.py
echo [%date% %time%] Agent4 DONE >> logs\scheduler.log
