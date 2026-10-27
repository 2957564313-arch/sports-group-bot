@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   正在启动运动群机器人...
echo   请保持本窗口运行，勿关闭
echo ========================================
".venv\Scripts\python.exe" bot.py
pause
