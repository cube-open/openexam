@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   在线考试系统 - 一键启动
echo ============================================
echo.

python -c "import flask, waitress" >nul 2>nul
if errorlevel 1 (
    echo [1/3] 首次运行，正在安装依赖...
    python -m pip install -r requirements.txt
    echo.
) else (
    echo [1/3] 依赖已就绪
)

echo [2/3] 初始化 / 升级数据库...
python -c "from examsys.db import init_db; init_db()"
echo.

echo [3/3] 启动服务（上线模式）...
echo.
python run.py

pause
