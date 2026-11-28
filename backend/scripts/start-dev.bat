@echo off
chcp 65001 >nul
setlocal

REM ================================================================
REM  一键起本地开发环境：后端 (uvicorn --reload) + 前端 (vite)
REM
REM  用法：双击，或在任意目录运行
REM        backend\scripts\start-dev.bat
REM
REM  为什么需要这个脚本，而不是两个手敲的命令行：
REM
REM  1. backend\.env **不会自动加载**。代码里没有 python-dotenv，也没有任何
REM     load_dotenv 调用（`backend/.env.example` 开头就写着"此文件不会自动
REM     加载"）。以前本地跑真模型得先手工 set 十几个变量，而漏一个的表现是
REM     "怎么又是演示图"或"没配置模型密钥"——两种都不像配置错误，像功能坏了。
REM
REM  2. 两个服务必须各自活在一个窗口里。放到同一个窗口后台跑的话，Ctrl+C 只
REM     停得了一个，另一个（尤其 uvicorn --reload 的 worker 与 vite 的 esbuild）
REM     会留下孤儿进程继续占端口，下次启动就是"端口被占用"。
REM
REM  3. 顺序有意义：vite 的 /api 代理指向 127.0.0.1:8000（见
REM     frontend/vite.config.ts）。后端没起来时前端页面打得开，但每个接口都
REM     502——看起来像前端坏了。所以这里等后端健康了再起前端。
REM
REM  停服务：关掉那两个窗口。
REM ================================================================

REM ---- 可调参数 --------------------------------------------------
REM 后端端口必须与 frontend/vite.config.ts 里 proxy 的目标一致。改这里就要
REM 同时改那一处，否则前端所有请求 502。
set "BACKEND_PORT=8000"
REM 前端端口只是 vite 的默认值。被占用时 vite 会自己换一个，末尾会提示看窗口。
set "FRONTEND_PORT=5173"
REM 等后端就绪的秒数。首次启动要 import 全部模块并建库，冷启动约 5-15 秒。
set "HEALTH_WAIT_SECONDS=60"

REM ---- 定位目录（%~dp0 结尾带反斜杠，用 for 规范化成绝对路径）------
for %%I in ("%~dp0..") do set "BACKEND_DIR=%%~fI"
for %%I in ("%BACKEND_DIR%\..") do set "ROOT_DIR=%%~fI"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "VENV_PY=%BACKEND_DIR%\.venv\Scripts\python.exe"
set "ENV_FILE=%BACKEND_DIR%\.env"

REM ---- 前置检查：缺什么就说清楚缺什么 ------------------------------
if not exist "%VENV_PY%" (
    echo [错误] 找不到后端虚拟环境：%VENV_PY%
    echo        先建一个：cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    goto :fail
)
if not exist "%FRONTEND_DIR%\node_modules" (
    echo [错误] 前端依赖没装：%FRONTEND_DIR%\node_modules 不存在
    echo        先装：cd frontend ^&^& npm install
    goto :fail
)

REM ---- 加载 backend\.env -----------------------------------------
REM 逐行 KEY=VALUE。规则只覆盖 .env.example 里实际出现的那一小部分：
REM   - 行首 # 是注释，空行跳过
REM   - 只按**第一个** = 切分，值里可以再有 = （比如带参数的 URL）
REM   - 值为空时**不设置**。这一条是有意的：.env.example 里满是 KEY= 这种空
REM     占位行，照抄成 .env 之后如果把空值也 set 进去，就会顶掉代码里的缺省
REM     值（MINDMAP_AGENT_ENABLED 会变成非数字、MINDMAP_PG_DSN 会变成空串）
REM     ——空串尤其坑：`app/db/pool.py` 是 `getenv(...) or DEFAULT_DSN`，空串会
REM     被当成"配了但配错了"，而不是"没配，用缺省的本机 socket"。
REM 不支持：行尾注释、多行值、${VAR} 插值。.env.example 里都不出现。
if not exist "%ENV_FILE%" (
    echo [提示] 没有 %ENV_FILE%，跳过加载。
    echo        将只用系统环境变量与代码缺省值；要真实模型就：
    echo            copy backend\.env.example backend\.env   然后填密钥
    goto :env_done
)
echo 读取 %ENV_FILE%
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if not "%%~A"=="" if not "%%~B"=="" set "%%~A=%%~B"
)
:env_done

REM ---- 端口检查：已经在跑了就别再开一份 ----------------------------
call :port_busy %BACKEND_PORT%
if not errorlevel 1 (
    echo [错误] 端口 %BACKEND_PORT% 已被占用，多半是上一次的后端还在跑。
    echo        关掉那个窗口，或者查：netstat -ano ^| findstr :%BACKEND_PORT%
    goto :fail
)
if not "%MINDMAP_LLM_MODE%"=="demo" if "%MINDMAP_LLM_API_KEY%"=="" (
    echo [提示] MINDMAP_LLM_MODE 不是 demo，但没读到密钥。
    echo        生成会在第一次调模型时报"没配置模型密钥"。
    echo        去 backend\.env 填 MINDMAP_LLM_API_KEY，或用页面的设置面板。
)

REM ---- 起后端 ----------------------------------------------------
echo.
echo [1/2] 后端  http://127.0.0.1:%BACKEND_PORT%
start "genai-mindmap . backend" /D "%BACKEND_DIR%" cmd /k ""%VENV_PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port %BACKEND_PORT%"

REM ---- 等后端健康 ------------------------------------------------
REM 用 curl 而不是 netstat：端口 LISTENING 只说明进程起来了，不代表能处理
REM 请求（建库、加载模型都可能还在第一步）。/api/health 是公开路径，不需要
REM 登录态。
where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [提示] 没有 curl.exe，跳过就绪检测，直接起前端。
    goto :backend_done
)
echo       等待后端就绪（最多 %HEALTH_WAIT_SECONDS% 秒）……
set /a WAITED=0
:wait_health
set /a WAITED+=1
curl.exe -s -f --max-time 2 -o nul "http://127.0.0.1:%BACKEND_PORT%/api/health" >nul 2>&1
if not errorlevel 1 goto :backend_ready
if %WAITED% geq %HEALTH_WAIT_SECONDS% goto :backend_timeout
timeout /t 1 /nobreak >nul 2>&1
if errorlevel 1 ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_health

:backend_ready
echo       后端已就绪（等了 %WAITED% 秒）
goto :backend_done

:backend_timeout
echo       [警告] 等了 %HEALTH_WAIT_SECONDS% 秒后端还没响应。
echo              去看 "backend" 那个窗口的报错——通常是缺依赖，或者 .env 里的
echo              模型地址/密钥不对。前端仍会启动，但页面上的接口会 502。

:backend_done

REM ---- 起前端 ----------------------------------------------------
echo.
echo [2/2] 前端  http://127.0.0.1:%FRONTEND_PORT%
start "genai-mindmap . frontend" /D "%FRONTEND_DIR%" cmd /k "npm run dev"

echo.
echo ================================================================
echo   前端      http://127.0.0.1:%FRONTEND_PORT%
echo             实际端口看 "frontend" 窗口（%FRONTEND_PORT% 被占用时 vite 会换）
echo   后端      http://127.0.0.1:%BACKEND_PORT%
echo   接口文档  http://127.0.0.1:%BACKEND_PORT%/docs
echo ================================================================
echo   改代码会自动重载。停服务：关掉上面那两个窗口。
echo.
endlocal
exit /b 0

REM ================================================================
REM  子程序
REM ================================================================
:port_busy
REM 返回 errorlevel 0 表示该端口正在 LISTENING。冒号与尾随空格一起匹配，
REM 否则 :8000 会命中 :80000。
netstat -ano -p TCP | findstr /R /C:":%~1 .*LISTENING" >nul 2>&1
exit /b %errorlevel%

:fail
echo.
echo 启动中止。按任意键关闭……
pause >nul
endlocal
exit /b 1
