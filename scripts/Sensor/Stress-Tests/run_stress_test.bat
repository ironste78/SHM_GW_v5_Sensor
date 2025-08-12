@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION

REM === Stress test launcher for Windows ===
REM Usage examples:
REM   run_stress_tests.bat            -> run all scenarios
REM   run_stress_tests.bat P5         -> run only P5
REM   run_stress_tests.bat P1,P2,A1   -> run P1,P2,A1
REM   run_stress_tests.bat P1 P2 A1   -> run P1,P2,A1 (spaces ok)

REM Move to this script folder so relative paths work
cd /d "%~dp0"

REM --------- Common settings (edit here if needed) ----------
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
REM ----------------------------------------------------------

REM --- provenance (metti i tuoi valori reali) ---
set REPO_URL=https://github.com/ironste78/SHM_GW_v5_Sensor.git
set COMMIT_SHA=e2024b0db796f42c3c7b20d55836d375aacf17a3
set BRANCH=main
set DIRTY=0

REM Gather scenarios from args; default to "all"
set "TEST_SCENARIOS=%*"
if not defined TEST_SCENARIOS set "TEST_SCENARIOS=all"

REM Normalize: replace spaces with commas (so "P1 P2" => "P1,P2")
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

endlocal
