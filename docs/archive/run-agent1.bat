@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent1 START >> logs\scheduler.log

:: Python数据采集
"%~dp0venv\Scripts\python.exe" -X utf8 scripts\agent1-情报采集\fetch_all.py
echo [%date% %time%] Agent1 DONE >> logs\scheduler.log
