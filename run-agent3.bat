@echo off
chcp 65001 >nul
cd /d "C:\Users\65004\Desktop\小白\股票投资"
:: Token 从 .claude/settings.local.json 自动加载

if not exist logs mkdir logs
echo [%date% %time%] Agent3 START >> logs\scheduler.log

:: 风控检查
"C:\Users\65004\Desktop\小白\股票投资\venv\Scripts\python.exe" -X utf8 scripts\agent3-风控\risk_check.py --portfolio data\portfolio.json
echo [%date% %time%] Agent3 DONE >> logs\scheduler.log
