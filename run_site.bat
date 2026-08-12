@echo off
REM Gridiron Edge - start the local site if it is not already running.
REM Registered as Windows Scheduled Task "GridironEdge-Site" (logon + keep-alive).
setlocal
cd /d "%~dp0"

REM No-op if something is already LISTENING on the port, so the keep-alive
REM trigger cannot pile up duplicate servers.
netstat -ano | findstr /R /C:"LISTENING" | findstr /C:"127.0.0.1:5057" >nul
if %errorlevel%==0 (
  echo [%date% %time%] Site already running on 5057 >> "%~dp0logs\site.log"
  exit /b 0
)

REM pythonw = no console window. Same python.org interpreter as the fetch.
set PYW="C:\Users\Calvin Chan\AppData\Local\Programs\Python\Python311\pythonw.exe"

REM FF_NO_RELOAD stops Flask's reloader from forking a second process, which
REM would leave an orphan holding the port when the task restarts the service.
set FF_NO_RELOAD=1

echo [%date% %time%] Starting site on http://127.0.0.1:5057 >> "%~dp0logs\site.log"
start "" %PYW% "%~dp0app.py"
endlocal & exit /b 0
