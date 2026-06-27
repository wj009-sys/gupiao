@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent2 START >> logs\scheduler.log
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent2-技术分析\analyze.py
echo [%date% %time%] Agent2 DONE >> logs\scheduler.log
