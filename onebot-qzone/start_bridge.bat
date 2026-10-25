@echo off
chcp 65001 >nul
title QZone Bridge
cd /d "%~dp0"
node dist/main.js
echo 桥接已退出。
pause
