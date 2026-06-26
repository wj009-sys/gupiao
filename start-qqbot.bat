@echo off
chcp 65001 >nul
title QQ Bot 后台保活服务

echo ╔══════════════════════════════════════════╗
echo ║      QQ Bot 后台保活服务                  ║
echo ║      保持 QQ 机器人持久在线                ║
echo ╚══════════════════════════════════════════╝
echo.

:: 切换到项目目录
cd /d "%~dp0..\..\.."

:: 检查 Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [错误] 未找到 Node.js，请先安装
    pause
    exit /b 1
)

echo [信息] 启动 QQ Bot 保活服务...
echo [信息] 按 Ctrl+C 停止
echo.

:: 启动保活服务
node -e "
const fs = require('fs');
// 加载 settings.local.json
try {
    const cfg = JSON.parse(fs.readFileSync('.claude/settings.local.json', 'utf-8'));
    Object.assign(process.env, cfg.env || {});
} catch(e) {}
require('./.claude/mcp-servers/qqbot/keepalive.js');
" 2>&1

echo.
pause
