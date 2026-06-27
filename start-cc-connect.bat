@echo off
chcp 65001 >nul
title cc-connect - Claude Code 消息桥接
echo ============================================
echo  启动 cc-connect (Claude Code ↔ 微信/QQ/Telegram)
echo ============================================
echo.
echo 注意：请先在另一个窗口启动 NapCatQQ (start-napcat.bat)
echo.
echo 微信个人号：用手机扫码后即可使用
echo QQ：需要 NapCat 已登录并配置好 WebSocket
echo Telegram：需要配置 Bot Token
echo.
cd /d "%~dp0"
set PATH=%PATH%;%APPDATA%\npm;%APPDATA%\npm\node_modules\cc-connect\bin;%ProgramFiles%\nodejs
echo.
echo 检测 Claude Code...
where claude >nul 2>&1
if errorlevel 1 (
    echo [警告] 未找到 claude 命令，请确认 Claude Code 已安装
    echo.
)
echo.
echo 启动 cc-connect...（按 Ctrl+C 停止）
echo.
cc-connect --config cc-connect.toml
if errorlevel 1 (
    echo.
    echo 启动失败！
    echo 请确认:
    echo   1. npm install -g cc-connect
    echo   2. Claude Code 已安装 (npm install -g @anthropic-ai/claude-code)
    echo   3. 微信需先运行「微信扫码绑定.bat」
    echo   4. QQ 需先启动 start-napcat.bat
    pause
)
