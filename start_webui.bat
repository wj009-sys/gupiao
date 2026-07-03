@echo off
chcp 65001 >nul
title 股票投研 WebUI

set PROJECT_DIR=C:\Users\65004\Desktop\小白\股票投资
cd /d "%PROJECT_DIR%"

:: 确保 logs 目录存在
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

:: 激活虚拟环境
call "%PROJECT_DIR%\venv\Scripts\activate.bat"

:: 启动 WebUI（--reload 使代码修改自动生效）
echo [%date% %time%] 启动 WebUI: http://localhost:8080
uvicorn webui.main:app --host 0.0.0.0 --port 8080 --reload >> "%PROJECT_DIR%\logs\webui.log" 2>&1

pause
