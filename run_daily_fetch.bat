@echo off
REM Gridiron Edge - daily data refresh.
REM Registered as Windows Scheduled Task "GridironEdge-DailyFetch" (06:30).
setlocal
cd /d "%~dp0"
echo ============================================================ >> "%~dp0logs\fetch.log"
echo [%date% %time%] Starting daily fetch >> "%~dp0logs\fetch.log"

REM Use the real python.org install, NOT the Microsoft Store execution alias in
REM %LocalAppData%\Microsoft\WindowsApps — that alias is a 0-byte reparse point
REM that fails to launch under Task Scheduler's non-interactive session
REM (result 0x80070001), which has silently frozen other trackers on this box.
REM
REM NOTE: this interpreter has NO pandas and NO numpy. That is deliberate and
REM the whole pipeline is stdlib-only so it cannot matter. Do not add a
REM dependency here without installing it against THIS exe.
set PY="C:\Users\Calvin Chan\AppData\Local\Programs\Python\Python311\python.exe"

REM --- The fetch. Capture its exit code immediately; later commands overwrite
REM %errorlevel% and a green task would otherwise hide a crashed fetch. ---
%PY% -m data.fetch_all >> "%~dp0logs\fetch.log" 2>&1
set RC=%errorlevel%

REM --- DEFINITIVE gate: prove the data actually landed and the model still
REM builds, regardless of the exit code above. A fetcher can exit 0 having
REM written nothing, so freshness is verified against the database itself. ---
%PY% -m data.assert_fresh >> "%~dp0logs\fetch.log" 2>&1
if errorlevel 1 set RC=1

REM --- Rebuild the static site. The page reads docs/, not the database, so
REM without this the local site keeps serving yesterday's board until the
REM server happens to restart. ---
%PY% "%~dp0export_static.py" >> "%~dp0logs\fetch.log" 2>&1
if errorlevel 1 set RC=1

echo [%date% %time%] Finished with exit code %RC% >> "%~dp0logs\fetch.log"
endlocal & exit /b %RC%
