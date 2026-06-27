@echo off
chcp 65001 >nul
title NapCatQQ - 股票投研QQ机器人
echo ============================================
echo  启动 NapCatQQ (QQ机器人 + OneBot v11)
echo ============================================
echo.
echo 首次使用：
echo   1. 会弹出 QQ 登录窗口
echo   2. 用手机 QQ 扫码登录
echo   3. 登录后访问 http://localhost:6099/webui 配置 WebSocket
echo   4. 在 WebUI 中「网络配置」→ 新增 WebSocket 正向连接 → 端口 3001
echo   5. 重启本窗口
echo.
echo Windows 防火墙弹出时请允许访问
echo.
pause
cd /d "%~dp0napcat\bootmain"
.\NapCatWinBootMain.exe %1
if errorlevel 1 (
    echo.
    echo 启动失败！请确保：
    echo   1. NapCat 已完整安装
    echo   2. 如果首次运行，双击运行 napcat\NapCatInstaller.exe
    echo.
    pause
)
