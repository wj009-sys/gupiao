@echo off
chcp 65001 >nul
title 微信个人号 - cc-connect 绑定
echo ============================================
echo  微信个人号绑定 (cc-connect weixin setup)
echo ============================================
echo.
echo 请在手机微信中扫描二维码以绑定微信个人号
echo 二维码将显示在浏览器或终端中
echo.
pause
cd /d "%~dp0"
set PATH=%PATH%;%APPDATA%\npm;%APPDATA%\npm\node_modules\cc-connect\bin;%ProgramFiles%\nodejs
echo 正在启动微信绑定...（完成后会自动写入 Token）
echo.
cc-connect weixin setup --config cc-connect.toml --project stock-research
echo.
if errorlevel 1 (
    echo.
    echo 绑定失败！
    echo 请确认已安装: npm install -g cc-connect
    echo 如果网络受限，请使用代理或手机热点
    pause
) else (
    echo 绑定成功！现在可以启动 start-cc-connect.bat
    pause
)
