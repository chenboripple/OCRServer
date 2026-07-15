@echo off
REM ============================================
REM OCR Server 本地开发启动脚本 (Windows)
REM 功能: 自动检查并安装 OpenCodeReview (ocr CLI)
REM ============================================

echo ============================================
echo   OCR Server - Windows 本地开发启动
echo ============================================

REM 切换到脚本所在目录
cd /d "%~dp0"

REM ------------------------------
REM 检查 Python 虚拟环境
REM ------------------------------
if not exist ".venv" (
    echo [WARN] 未检测到 Python 虚拟环境，正在创建...
    python -m venv .venv
    echo [OK] 虚拟环境创建成功
)

REM 激活虚拟环境
call .venv\Scripts\activate.bat

REM 检查依赖安装
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Python 依赖未安装，正在安装...
    pip install -r requirements.txt
)

REM ------------------------------
REM 加载 .env 文件
REM ------------------------------
if exist ".env" (
    echo [INFO] 加载 .env 配置文件
    for /f "tokens=*" %%a in ('type .env ^| findstr /v "^#"') do (
        set %%a
    )
)

REM ------------------------------
REM 检查并安装 ocr CLI
REM ------------------------------
set OCR_VERSION=1.7.6

where ocr >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================
    echo   自动安装 OpenCodeReview CLI
    echo ============================================

    where npm >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] 未检测到 npm，请先安装 Node.js 20+
        echo         下载地址: https://nodejs.org/
        pause
        exit /b 1
    )

    echo 正在安装 @alibaba-group/open-code-review@%OCR_VERSION% ...
    npm install -g @alibaba-group/open-code-review@%OCR_VERSION%

    where ocr >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] ocr 安装失败，请检查网络连接或手动安装
        pause
        exit /b 1
    )

    for /f "tokens=*" %%i in ('ocr --version') do set OCR_VER=%%i
    echo [OK] ocr 安装成功: %OCR_VER%
) else (
    for /f "tokens=*" %%i in ('ocr --version') do set OCR_VER=%%i
    echo [OK] ocr 已安装: %OCR_VER%
)

REM ------------------------------
REM 启动服务
REM ------------------------------
echo.
echo ============================================
echo   启动开发服务器
echo ============================================
if "%OCR_SERVER_HOST%"=="" set OCR_SERVER_HOST=0.0.0.0
if "%OCR_SERVER_PORT%"=="" set OCR_SERVER_PORT=8000
echo 访问地址: http://%OCR_SERVER_HOST%:%OCR_SERVER_PORT%
echo 健康检查: http://%OCR_SERVER_HOST%:%OCR_SERVER_PORT%/health
echo API 文档: http://%OCR_SERVER_HOST%:%OCR_SERVER_PORT%/docs
echo ============================================
echo.

REM 使用 --reload 启动开发模式
uvicorn app.main:app --host %OCR_SERVER_HOST% --port %OCR_SERVER_PORT% --reload

pause
