@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent6 START >> logs\scheduler.log

:: 制定交易计划
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent6-操盘\trader.py
echo [%date% %time%] Agent6 DONE >> logs\scheduler.log
