@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION

REM === Path relativi rispetto a questo script ===
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%..\..\.."
set "DATA_DIR=%REPO_ROOT%\data"
set "VALIDATOR=%SCRIPT_DIR%..\validate_shm_files.py"

REM === Stress test launcher for Windows ===
REM Usage examples:
REM   run_stress_test.bat            -> run all scenarios
REM   run_stress_test.bat P5         -> run only P5
REM   run_stress_test.bat P1,P2,A1   -> run P1,P2,A1
REM   run_stress_test.bat P1 P2 A1   -> run P1,P2,A1 (spaces ok)

cd /d "%SCRIPT_DIR%"

REM --------- Common settings ----------
set "PY=python"
set "SENSOR_CMD=python main.py --config ..\exec\windows\config.ini --allow-unregistered"
set "CWD=D:\SHMSource\SHM_GW_v5\LocalVersion\@Sensor"
set "WORK_ROOT=."
set "CONFIG=.\stress_config_example.json"
set "DURATION=120"
set "BASE_PORT=5003"
set "BOARD_CMD=python ..\mock_udp_responder.py"
set "BOARD_ARGS=--uuid {uuid} --ip 127.0.0.1 --tcp-port 1105 --channels-map 11140000 --header-crc --jitter-us 1"
set "HTML=.\work\stress_report.html"
set "JSON=.\work\stress_report.json"
set "WAIT=20"
REM ------------------------------------

REM provenance
set REPO_URL=https://github.com/ironste78/SHM_GW_v5_Sensor.git
set COMMIT_SHA=7f324c787ab4e52555fad04b6b1e3affa8369f32
set BRANCH=main
set DIRTY=0

REM Scenari dai parametri
set "TEST_SCENARIOS=%*"
if not defined TEST_SCENARIOS set "TEST_SCENARIOS=all"
set "TEST_SCENARIOS=%TEST_SCENARIOS: =,%"

echo [launcher] Scenarios: %TEST_SCENARIOS%

if /I "%TEST_SCENARIOS%"=="all" (
  "%PY%" sensor_stress_runner.py ^
    --sensor-cmd "%SENSOR_CMD%" ^
    --cwd "%CWD%" ^
    --work-root "%WORK_ROOT%" ^
    --config "%CONFIG%" ^
    --all ^
    --duration %DURATION% ^
    --base-port %BASE_PORT% ^
    --board-mock-cmd "%BOARD_CMD%" ^
    --board-mock-args "%BOARD_ARGS%" ^
    --html-report "%HTML%" ^
    --report-json "%JSON%" ^
    --sensor-wait-seconds %WAIT%
) else (
  "%PY%" sensor_stress_runner.py ^
    --sensor-cmd "%SENSOR_CMD%" ^
    --cwd "%CWD%" ^
    --work-root "%WORK_ROOT%" ^
    --config "%CONFIG%" ^
    --only %TEST_SCENARIOS% ^
    --duration %DURATION% ^
    --base-port %BASE_PORT% ^
    --board-mock-cmd "%BOARD_CMD%" ^
    --board-mock-args "%BOARD_ARGS%" ^
    --html-report "%HTML%" ^
    --report-json "%JSON%" ^
    --sensor-wait-seconds %WAIT%
)

REM --- Se il runner fallisce ---
if errorlevel 1 (
  echo [launcher] Tests failed (code %ERRORLEVEL%)
  endlocal & exit /b %ERRORLEVEL%
)

REM --- Trova Python ---
where %PY% >nul 2>&1
if errorlevel 1 (
  where python >nul 2>&1 && (set "PY=python") ^
  || ( where py >nul 2>&1 && (set "PY=py -3") ^
  || ( echo [ERROR] Python non trovato & endlocal & exit /b 2 ))
)

REM --- Validazione file shm ---
set "VAL_JSON=%WORK_ROOT%\work\validation.json"
if not exist "%WORK_ROOT%\work" mkdir "%WORK_ROOT%\work" >nul 2>&1
echo [validate] Avvio: "%DATA_DIR%" -> "%VAL_JSON%"
"%PY%" "%VALIDATOR%" "%DATA_DIR%" ^
  --pattern "shm_*_05_*_*" ^
  --acc-range -16 16 --temp-range -40 125 ^
  --json-out "%VAL_JSON%" --strict

set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
