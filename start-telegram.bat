@echo off
chcp 65001 >nul
title Telegram 常驻监听服务

echo ╔══════════════════════════════════════════╗
echo ║      Telegram 常驻监听服务                ║
echo ║      自动响应手机命令                      ║
echo ╚══════════════════════════════════════════╝
echo.

:: 切换到项目目录
cd /d "%~dp0"

:: 检查 Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [错误] 未找到 Node.js，请先安装
    pause
    exit /b 1
)

echo [信息] 启动 Telegram 常驻监听服务...
echo [信息] 按 Ctrl+C 停止
echo.

:: 启动保活服务
node -e "
const fs = require('fs');
try {
    const cfg = JSON.parse(fs.readFileSync('.claude/settings.local.json', 'utf-8'));
    Object.assign(process.env, cfg.env || {});
} catch(e) {}
require('./.claude/mcp-servers/telegram/keepalive.js');
" 2>&1

echo.
pause
