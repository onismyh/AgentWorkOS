@echo off
rem AgentWorkOS CLI launcher: `awos <args>` -> .venv\Scripts\python.exe -X utf8 -m awos.cli <args>
rem Paths are derived from %~dp0 only (the repo path contains spaces and non-ASCII characters).
setlocal
set "AWOS_OLDCP="
for /f "tokens=2 delims=:" %%A in ('chcp') do set "AWOS_OLDCP=%%A"
if defined AWOS_OLDCP set "AWOS_OLDCP=%AWOS_OLDCP: =%"
if defined AWOS_OLDCP set "AWOS_OLDCP=%AWOS_OLDCP:.=%"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "AWOS_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%AWOS_PY%" (
  echo AgentWorkOS: "%AWOS_PY%" not found - run scripts\install.ps1 first. 1>&2
  set "AWOS_RC=9009"
  goto :done
)
"%AWOS_PY%" -X utf8 -m awos.cli %*
set "AWOS_RC=%ERRORLEVEL%"
:done
if defined AWOS_OLDCP chcp %AWOS_OLDCP% >nul
endlocal & exit /b %AWOS_RC%
