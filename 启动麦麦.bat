@echo off
chcp 65001 >nul
title MaiBot - 麦麦
cd /d "%~dp0my-maibot"
set "EULA_AGREE=8e6e7d647f7f82d6ea98456b73908656"
set "PRIVACY_AGREE=91e5db7659c560bc3545e63859b6ebc0"
echo 正在启动 MaiBot（麦麦）... WebUI: http://127.0.0.1:8001
C:\Users\29688\.local\bin\maibot.exe run --directory .
echo.
echo MaiBot 已退出。检查上方日志排查原因。
pause
