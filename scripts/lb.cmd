@echo off
chcp 65001 >nul
setlocal
set "HERMES_ROOT=%HERMES_HOME%"
if "%HERMES_ROOT%"=="" set "HERMES_ROOT=%LOCALAPPDATA%\hermes"
set "BIN=%HERMES_ROOT%\hermes-agent\venv\Scripts\hermes.exe"
set "OFF=%HERMES_ROOT%\loop_breaker.off"
set "TOK=%HERMES_ROOT%\loop_breaker.token_off"
set "ONF=%HERMES_ROOT%\loop_breaker.on"

set "MODE=%~1"
set "PERSIST=%~2"
if /I "%MODE%"=="" set "MODE=toggle"
if /I "%MODE%"=="toggle" (
  if exist "%OFF%" (set "MODE=on") else (set "MODE=off")
)

if /I "%MODE%"=="on" (
  if exist "%OFF%" del /q "%OFF%"
  if exist "%TOK%" del /q "%TOK%"
  type nul > "%ONF%"
) else if /I "%MODE%"=="token" (
  if exist "%OFF%" del /q "%OFF%"
  if exist "%ONF%" del /q "%ONF%"
  type nul > "%TOK%"
) else if /I "%MODE%"=="off" (
  if exist "%TOK%" del /q "%TOK%"
  if exist "%ONF%" del /q "%ONF%"
  type nul > "%OFF%"
) else if /I "%MODE%"=="clear" (
  if exist "%OFF%" del /q "%OFF%"
  if exist "%TOK%" del /q "%TOK%"
  if exist "%ONF%" del /q "%ONF%"
) else if /I NOT "%MODE%"=="status" (
  echo [lb] unknown arg "%MODE%"  ^(use on^|off^|token^|clear^|status^|toggle^)
  pause
  exit /b 1
)

if /I "%PERSIST%"=="--persist" (
  if /I "%MODE%"=="off" (
    "%BIN%" config set agent.loop_breaker.enabled false
  ) else (
    "%BIN%" config set agent.loop_breaker.enabled true
  )
)

echo.
echo ==========================================
echo   Loop Breaker 熔断开关
echo ==========================================
echo   哨兵目录 : %HERMES_ROOT%
echo   当前模式 : %MODE%
echo ------------------------------------------
if exist "%OFF%" echo   [X] 全关         loop_breaker.off
if exist "%TOK%" echo   [X] 只关Token点  loop_breaker.token_off
if exist "%ONF%" echo   [X] 强制开启     loop_breaker.on
if not exist "%OFF%" if not exist "%TOK%" if not exist "%ONF%" echo   [ ] 无哨兵 - 跟随 config.yaml
echo ------------------------------------------
if exist "%OFF%" (
  echo.
  echo   ****** 警告: 防死循环已关闭 ******
  echo   长任务可能无限运行，不会被自动停止。
  echo.
  echo   复位请运行: lb.cmd on
  echo   ^(或再次双击桌面「熔断开关」^)
  echo.
)
if exist "%TOK%" echo   提示: 仅 Token 确认点静音, 防死循环仍生效
echo.
echo   生效时机: 当前对话下一轮立即生效 ^(无需重启 Hermes^)
echo ==========================================
pause
