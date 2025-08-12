#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Runner scenari di stress per il Sensor.

Novità:
- Workdir scenari sotto <Stress-Tests>\work\<scenario-hash>\
- Config esterna (JSON o YAML) per timeouts, pattern, ordine scenari, args stressor
- Report HTML con indicatori verdi/rossi + link ai log

Esempi:
  py sensor_stress_runner.py ^
    --sensor-cmd "py main.py --config ..\..\..\exec\windows\config.ini" ^
    --cwd "D:\SHMSource\SHM_GW_v5\LocalVersion\@Sensor" ^
    --work-root "." ^
    --config ".\stress_config.json" ^
    --all ^
    --html-report ".\work\stress_report.html" ^
    --report-json ".\work\stress_report.json"

Oppure (solo alcuni scenari):
  py sensor_stress_runner.py --sensor-cmd "py main.py --config ..\..\..\exec\windows\config.ini" ^
    --cwd "D:\SHMSource\SHM_GW_v5\LocalVersion\@Sensor" --work-root "." --config ".\stress_config.json" ^
    --only S2 P1 --html-report ".\work\stress_report.html"
"""

import argparse, os, sys, subprocess, time, shutil, uuid, re, signal, json, html
import shlex
from typing import Any, Dict, List, Optional

HERE = os.path.abspath(os.path.dirname(__file__))
DEFAULT_WORK_ROOT = os.path.join(HERE, "work")
STRESSOR = os.path.join(HERE, "tcp_frame_stressor.py")

# -------------------- Config --------------------

_BUILTIN_CONFIG = {
    "scenario_order": ["S1", "S2", "S3", "S4", "S5", "P1", "P2", "P3", "P4", "P5", "T1", "A1", "A2"],
    "scenarios": {
        # S1 non usa lo stressor: verifichiamo accept-timeout senza client
        "S1": {
            "name": "No client / accept-timeout",
            "sensor_wait_seconds": 25,
            "patterns": [
                {"re": r"Accept timeout: no client connected", "min": 1},
                {"re": r"Board stop\+reset due to accept-timeout", "min": 1}
            ]
        },
        "S2": {
            "name": "Connect/Disconnect flapping",
            "stressor": {"scenario": "S2", "args": {"duration": 12}},
            "patterns": [
                {"re": r"Connection closed by the client", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "S3": {
            "name": "Jitter elevato",
            "stressor": {"scenario": "S3", "args": {"duration": 12}},
            "patterns": [
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "S4": {
            "name": "Pausa lunga (socket vivo)",
            "stressor": {"scenario": "S4", "args": {"warmup": 2.0, "pause": 10.0, "tail": 2.0}},
            "patterns": [
                {"re": r"Timeout while reading data|Connection closed by the client", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "S5": {
            "name": "Throughput elevato",
            "stressor": {"scenario": "S5", "args": {"duration": 8}},
            "patterns": [
                {"re": r"queue full → dropped .*", "min": 0},  # può essere 0 o più
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "P1": {
            "name": "Frame spezzati (split)",
            "stressor": {"scenario": "P1", "args": {"nframes": 50, "split": 5}},
            "patterns": [
                {"re": r"payload len .* != expected .*", "min": 0},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "P2": {
            "name": "Garbage tra i frame",
            "stressor": {"scenario": "P2", "args": {"nframes": 20, "garbage": 64}},
            "patterns": [
                {"re": r"buffer trimmed to .* cap reached", "min": 0},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "P3": {
            "name": "Header-only intermittente",
            "stressor": {"scenario": "P3", "args": {"nheaders": 20}},
            "patterns": [
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "P4": {
            "name": "nreports variabile",
            "stressor": {"scenario": "P4", "args": {"seq": [4, 10, 4, 8, 6]}},
            "patterns": [
                {"re": r"Number of reports differs", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "P5": {
            "name": "Oversize/Undersize payload",
            "stressor": {"scenario": "P5", "args": {}},
            "patterns": [
                {"re": r"payload len .* != expected .*", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "T1": {
            "name": "Timestamp in microsecondi",
            "stressor": {"scenario": "T1", "args": {"nframes": 20}},
            "patterns": [
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "A1": {
            "name": "STA/LTA prolungato senza FFT",
            "stressor": {"scenario": "A1", "args": {"duration": 35}},
            "patterns": [
                {"re": r"STA/LTA flag entered in pre-alarm state", "min": 1},
                {"re": r"Resetting STA_LTA flag", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "A2": {
            "name": "FFT prolungato senza STA/LTA",
            "stressor": {"scenario": "A2", "args": {"duration": 35}},
            "patterns": [
                {"re": r"FFT flag set", "min": 1},
                {"re": r"Resetting FFT flag", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        },
        "A3": {
            "name": "Alarm Triggered",
            "stressor": {"scenario": "A2", "args": {"pre": 3.0, "post": 3.0}},
            "patterns": [
                {"re": r"STA/LTA flag entered in pre-alarm state", "min": 1},
                {"re": r"FFT flag set", "min": 1},
                {"re": r"Triggering alarm on channel .*", "min": 1},
                {"re": r"Creating a new event data buffer", "min": 1},
                {"re": r"Sending triggered buffer to the alarm handler", "min": 1},
                {"re": r"Unhandled|Traceback", "max": 0}
            ]
        }
    }
}


def _read_tail(path: str, max_lines: int = 3, max_bytes: int = 8192) -> str:
    """
    Ritorna le ultime `max_lines` non-vuote del file `path`.
    Legge al massimo `max_bytes` dal fondo per evitare file grandi.
    """
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-max_lines:]) if lines else ""
    except Exception as e:
        return f"<tail read error: {e}>"


def parse_console_port(args_str: str, default: int = 1105) -> int:
    import re
    m = re.search(r"--tcp-port\s+(\d+)", args_str or "")
    return int(m.group(1)) if m else default

def print_step(msg: str):
    print(f"[runner] {msg}", flush=True)


def _load_yaml_or_json(path: str) -> Dict[str, Any]:
    if path.lower().endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
        except Exception as e:
            raise RuntimeError(f"Per leggere YAML installa 'pyyaml' oppure usa JSON. Errore: {e}")
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    else:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return _BUILTIN_CONFIG
    cfg = _load_yaml_or_json(path)
    # fallback dei campi mancanti dal builtin
    out = json.loads(json.dumps(_BUILTIN_CONFIG))  # deep copy
    out.update({k: v for k, v in cfg.items() if k != "scenarios"})
    if "scenarios" in cfg:
        out["scenarios"].update(cfg["scenarios"])
    return out

# -------------------- Utility --------------------

def run_cmd(cmd: str, cwd=None, env=None, detach=False) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(cmd, cwd=cwd, env=env, shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=creationflags)
                            

def wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection((host, port), 0.5)
            s.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False

def wait_server_opened(log_dir: str, port: int, timeout: float = 20.0) -> bool:
    """Attende che nel log compaia 'Socket server opened at ...:port'."""
    import time, glob, re
    end = time.time() + timeout
    patt = re.compile(rf"Socket server opened at .*:{port}", re.IGNORECASE)
    # trova il file log (può comparire qualche secondo dopo)
    path = None
    while time.time() < end:
        if not path:
            path = find_sensor_log(log_dir)
            if not path:
                time.sleep(0.2)
                continue
        s = tail_file(path, nbytes=200_000)
        if patt.search(s):
            return True
        time.sleep(0.2)
    return False

def wait_log_regex(log_dir: str, regex: str, timeout: float = 20.0) -> bool:
    """Attende che nel log del Sensor compaia una riga che matcha 'regex'."""
    import time, re
    end = time.time() + timeout
    patt = re.compile(regex, re.IGNORECASE)
    path = None
    while time.time() < end:
        if not path:
            path = find_sensor_log(log_dir)
            if not path:
                time.sleep(0.2)
                continue
        s = tail_file(path, nbytes=200_000)
        if patt.search(s):
            return True
        time.sleep(0.2)
    return False

def wait_packet_handler_started(log_dir: str, timeout: float = 30.0) -> bool:
    # il log reale è: "[PacketHandler] Packet handler is running..."
    return wait_log_regex(log_dir, r"\[PacketHandler\]\s*Packet handler is running\.\.\.", timeout=timeout)


def _safe_close(fh):
    try:
        if fh:
            try: fh.flush()
            except Exception: pass
            fh.close()
    except Exception:
        pass
        
def kill_proc(proc: subprocess.Popen):
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # tentativo “gentile” al process group (se creato)
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                time.sleep(0.5)
            except Exception:
                pass
            # termina il processo
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                pass
            # se è ancora vivo, usa taskkill per il tree
            if proc.poll() is None:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
                    )
                except Exception:
                    pass
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    except Exception:
        pass

def tail_file(path: str, nbytes: int = 400_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""

def find_sensor_log(log_dir: str) -> Optional[str]:
    for name in os.listdir(log_dir):
        if name.startswith("sensor_") and name.endswith(".log"):
            return os.path.join(log_dir, name)
    return None

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def html_report(path: str, results: List[Dict[str, Any]]):
    ensure_dir(os.path.dirname(path))
    # CSS minimale con pallini verdi/rossi
    css = """
    body { font-family: Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }
    h1 { margin-top: 0; }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
    .status { font-weight: 600; }
    .PASS { color: #0a8a0a; }
    .FAIL { color: #b30000; }
    .ERROR { color: #b36b00; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
    .dot.pass { background: #15c215; }
    .dot.fail { background: #e01616; }
    .dot.err { background: #f3a400; }
    details { margin-top: 6px; }
    code { background: #f7f7f7; padding: 1px 4px; border-radius: 3px; }
    .stderr-tail { max-width: 520px; max-height: 8em; overflow: auto; font-size: 12px; background: #222; color: #fafafa; padding: 6px 8px; border-radius: 6px; }
    .small { color: #666; font-size: 12px; }
    """
    # body
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    errored = sum(1 for r in results if r["status"] == "ERROR")
    rows = []
    for r in results:
        dot_class = "pass" if r["status"] == "PASS" else ("fail" if r["status"] == "FAIL" else "err")
        log_link = html.escape(r.get("log",""))
        checks_html = ""
        for c in r.get("checks", []):
            patt = c.get("pattern", c.get("re",""))
            matched = c.get("matched", False)
            count = c.get("count", None)
            minv = c.get("min", None)
            maxv = c.get("max", None)
            line = f"<code>{html.escape(patt)}</code> → "
            if count is not None:
                line += f"count={count} "
            line += ("✅" if matched else "❌")
            if minv is not None: line += f" (min={minv})"
            if maxv is not None: line += f" (max={maxv})"
            checks_html += f"<div class='small'>{line}</div>"
        tail = r.get("stressor_error_tail") or r.get("mock_error_tail") or r.get("diagnostic") or ""
        html_tail = f'<pre class="stderr-tail">{tail}</pre>' if tail else ""
        reason = html.escape(r.get("reason",""))        
        rows.append(f"""
          <tr>
            <td>{html.escape(r['scenario'])}</td>
            <td class="status {r['status']}"><span class="dot {dot_class}"></span>{r['status']}</td>
            <td>{html.escape(r.get('name',''))}<details><summary>Dettagli</summary>
                <div class="small">UUID: <code>{html.escape(r.get('uuid',''))}</code></div>
                <div class="small">Reason: {reason}</div>
                <div class="small">Log: <code>{log_link}</code></div>
                {checks_html}
            </details></td>
            <td>{html_tail}</td>
          </tr>
        """)
    body = f"""
    <html><head><meta charset="utf-8"><title>Sensor Stress Report</title>
    <style>{css}</style></head><body>
    <h1>Sensor Stress Report</h1>
    <div>Summary: <b>{passed} PASS</b> · <b>{failed} FAIL</b> · <b>{errored} ERROR</b> / {len(results)}</div>
    <table>
      <thead><tr><th>Scenario</th><th>Esito</th><th>Dettagli</th><th>stderr tail</th></tr></thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    </body></html>
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)

# -------------------- Core --------------------
def run_scenario(sensor_cmd: str, port: int, scenario: str, sc_def: Dict[str, Any],
                 cwd_sensor: str, work_root: str, default_duration: float = 12.0, sensor_wait_seconds: float = 0.0,
                 board_mock_cmd: Optional[str] = None, board_mock_args: Optional[str] = None, stressor_extra="") -> Dict[str, Any]:
    sensor = stress = mock_proc = None
    sensor_out = sensor_err = mock_out = mock_err = None
                     
    sid = f"{scenario}-{uuid.uuid4().hex[:8]}"
    uuid_str = f"test-{sid}"
    work_dir = os.path.abspath(os.path.join(work_root, f"{sid}"))
    log_dir = os.path.join(work_dir, "logs")
    ensure_dir(log_dir)

    try:
        # avvio mock della board (console TCP) se richiesto
        console_port = parse_console_port(board_mock_args or "")
        if board_mock_cmd:
            mock_cmd = f'{board_mock_cmd} {(board_mock_args or "").format(uuid=uuid_str)}'
            mock_env = os.environ.copy()
            mock_out_path = os.path.join(work_dir, "mock_stdout.log")
            mock_err_path = os.path.join(work_dir, "mock_stderr.log")
            mock_out = open(os.path.join(work_dir, "mock_stdout.log"), "wb")
            mock_err = open(os.path.join(work_dir, "mock_stderr.log"), "wb")
            print_step(f"Starting board mock: {mock_cmd}")
            mock_proc = subprocess.Popen(mock_cmd, cwd=HERE, env=mock_env, shell=True,
                                         stdout=mock_out, stderr=mock_err,
                                         creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0))
                                         
            print_step(f"Waiting board mock on tcp://127.0.0.1:{console_port} ...")
            if not wait_port("127.0.0.1", console_port, timeout=15.0):
                tail = _read_tail(mock_err_path, max_lines=10)
                kill_proc(mock_proc)
                return {
                    "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR",
                    "reason": f"Board mock not listening on {console_port}",
                    "log": mock_out_path,
                    "mock_error_tail": tail
                }

        # Avvio Sensor
        env = os.environ.copy()
        env["LOG_DIR"] = log_dir
        env["RUN_DIR"] = os.path.join(work_dir, "run")
        env["SENSOR_ALLOW_UNREGISTERED"] = "1"
        ensure_dir(env["RUN_DIR"])


        sensor_out_path = os.path.join(work_dir, "sensor_stdout.log")
        sensor_err_path = os.path.join(work_dir, "sensor_stderr.log")
        sensor_out = open(os.path.join(work_dir, "sensor_stdout.log"), "wb")
        sensor_err = open(os.path.join(work_dir, "sensor_stderr.log"), "wb")

        cmd = f'{sensor_cmd} -su {uuid_str} -sp {port} -sh 127.0.0.1'
        print_step(f"Starting Sensor: (cwd={cwd_sensor}) {cmd}")
        sensor = subprocess.Popen(cmd, cwd=cwd_sensor, env=env, shell=True,
                                  stdout=sensor_out, stderr=sensor_err,
                                  creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0))

        # fail-fast se il processo termina subito
        time.sleep(0.8)
        if sensor.poll() is not None:
            # legge gli ultimi 200k dei file stdout/err per diagnosi
            diag = ""
            try:
                diag = (tail_file(sensor_out_path) + "\n" + tail_file(sensor_err_path)).strip()
            except Exception:
                pass
            if mock_proc: kill_proc(mock_proc)
            return {"scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR", "reason": "Sensor exited prematurely", "log": sensor_err_path, "diagnostic": diag}

        print_step("Waiting for 'Socket server opened ...' marker in Sensor log ...")
        if not wait_server_opened(log_dir, port, timeout=30.0):
            kill_proc(sensor);  kill_proc(mock_proc) if mock_proc else None
            return {"scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR", "reason": "Sensor did not open server (no log marker)",
                    "log": find_sensor_log(log_dir) or sensor_err_path}
        t_open_seen = time.time()

        time.sleep(1.2)
        
        print_step(f"Checking tcp://127.0.0.1:{port} reachable ...")
        if not wait_port("127.0.0.1", port, timeout=5.0):
            try:
                if not wait_port("::1", port, timeout=2.0):
                    raise RuntimeError("no v6 either")
            except Exception:
                kill_proc(sensor);  kill_proc(mock_proc) if mock_proc else None
                return {"scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                        "status": "ERROR", "reason": "Sensor port not open",
                        "log": find_sensor_log(log_dir) or sensor_err_path}

        # NEW: attendi che il PacketHandler sia operativo prima di lanciare lo stressor
        print_step("Waiting for '[PacketHandler] Packet handler is running...' marker in Sensor log ...")
        if not wait_packet_handler_started(log_dir, timeout=30.0):
            kill_proc(sensor);  kill_proc(mock_proc) if mock_proc else None
            return {"scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR", "reason": "Packet handler never started (no log marker)",
                    "log": find_sensor_log(log_dir) or sensor_err_path}


        if sensor_wait_seconds and sensor_wait_seconds > 0:
            accept_to = int(os.environ.get("SENSOR_ACCEPT_TIMEOUT", "20"))
            elapsed = time.time() - t_open_seen
            margin  = 5  # secondi di safety
            budget  = max(0, accept_to - margin - elapsed)

            eff_wait = min(sensor_wait_seconds, budget)
            print_step(f"Sleeping {eff_wait:.0f}s "
                  f"(elapsed since server-open={elapsed:.1f}s, "
                  f"accept-timeout={accept_to}s, margin={margin}s) "
                  "before launching stressor ...")
            time.sleep(eff_wait)

        #if sensor_wait_seconds and sensor_wait_seconds > 0:
        #    print_step(f"Sleeping {sensor_wait_seconds:.1f}s before launching stressor ...")
        #    time.sleep(sensor_wait_seconds)


        # Esecuzione scenario
        if sc_def.get("stressor"):
            st = sc_def["stressor"]
            st_scenario = st.get("scenario", scenario)
            st_args = st.get("args", {})
            # Se non specificato, duration default
            if "duration" not in st_args and scenario in ("S2","S3","S5","A1"):
                st_args["duration"] = default_duration

            # compose command
            py = sys.executable or "python"
            args_list = ["--host", "127.0.0.1", "--port", str(port), "--scenario", st_scenario]

            # flatten args dict (True -> --flag, False/None -> niente, value -> --flag value, list -> ripetuto)
            def _dash(s: str) -> str:
                return s.replace("_", "-")

            for k, v in st_args.items():
                flag = f"--{_dash(str(k))}"
                if isinstance(v, bool):
                    if v:
                        args_list.append(flag)                 # solo flag (es. --epoch-payload)
                elif v is None or v == "":
                    continue
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        args_list += [flag, str(item)]
                else:
                    args_list += [flag, str(v)]

            # eventuali extra passati “raw” (stringa) -> splittati in modo sicuro
            extra_list = shlex.split(stressor_extra) if stressor_extra else []

            # comando finale come lista (no shell=True => niente problemi di quoting)
            cmd_list = [py, STRESSOR] + args_list + extra_list

            st_out_path = os.path.join(work_dir, "stressor_stdout.log")
            st_err_path = os.path.join(work_dir, "stressor_stderr.log")
            st_out = open(st_out_path, "wb")
            st_err = open(st_err_path, "wb")

            try:
                creationflags = 0
                if os.name == "nt":
                    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

                stress = subprocess.Popen(
                    cmd_list, cwd=HERE, shell=False,
                    stdout=st_out, stderr=st_err,
                    creationflags=creationflags
                )

                # log del comando effettivo
                try:
                    printable = shlex.join(cmd_list)  # Py3.8+
                except AttributeError:
                    printable = " ".join(cmd_list)
                print(f"[runner] Stressor started: {printable}")
                # opzionale: log anche della mappa args interpretata
                print(f"[runner] Stressor args map: {st_args}")

                timeout = float(sc_def.get("timeout", 0)) or (default_duration + 90)
                stress.wait(timeout=timeout)
                
                st_rc = stress.returncode if stress else None
                if st_rc not in (0, None):
                    result = {
                        "scenario": scenario,
                        "name": sc_def.get("name",""),
                        "uuid": uuid_str,
                        "status": "ERROR",
                        "reason": f"Stressor exited with code {st_rc}",
                        "log": find_sensor_log(log_dir) or sensor_err_path,
                        "sensor_wait_seconds": sensor_wait_seconds,
                        "stressor_stderr": st_err_path
                    }
                    # chiudi sensatamente mock+sensor
                    kill_proc(sensor)
                    if mock_proc: kill_proc(mock_proc)
                    # aggiungi informazioni sul motivo dell'errore
                    tail = _read_tail(st_err_path, max_lines=3)
                    if tail:
                        result["stressor_error_tail"] = tail                    
                    return result
            except Exception:
                try:
                    if stress: stress.kill()
                except Exception:
                    pass
            finally:
                try: st_out.close()
                except Exception: pass
                try: st_err.close()
                except Exception: pass
        else:
            # scenario S1: niente client; attendi per il tempo configurato
            print(f"[runner] No stressor configured for scenario {scenario} — skipping stressor launch")
            wait_s = float(sc_def.get("sensor_wait_seconds", 25))
            time.sleep(wait_s)

        # permetti al log di drenare
        time.sleep(2.0)

        # chiusura Sensor
        kill_proc(sensor)

        # chiudi il mock, se avviato
        if mock_proc:
            kill_proc(mock_proc)
            
        # leggi log
        log_path = find_sensor_log(log_dir)
        if not log_path:
            return {"scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR", "reason": "Log file not found"}

        content = tail_file(log_path)
        # verifica pattern
        passed = True
        details = []
        for patt in sc_def.get("patterns", []):
            rx = patt.get("re") or patt.get("pattern")
            if not rx:
                continue
            minv = patt.get("min", None)
            maxv = patt.get("max", None)
            # count occorrenze (case-insensitive)
            count = len(re.findall(rx, content, flags=re.IGNORECASE))
            ok = True
            if minv is not None and count < int(minv):
                ok = False
            if maxv is not None and count > int(maxv):
                ok = False
            details.append({"pattern": rx, "count": count, "matched": ok, "min": minv, "max": maxv})
            if not ok:
                passed = False

        return {
            "scenario": scenario,
            "name": sc_def.get("name",""),
            "uuid": uuid_str,
            "status": "PASS" if passed else "FAIL",
            "log": log_path,
            "checks": details,
            "sensor_wait_seconds": sensor_wait_seconds,
        }
        
    finally:
        # terminiamo i processi rimasti
        try: kill_proc(stress)
        except Exception: pass
        try: kill_proc(sensor)
        except Exception: pass
        try: kill_proc(mock_proc)
        except Exception: pass

        # chiudiamo SEMPRE i file
        _safe_close(sensor_out)
        _safe_close(sensor_err)
        _safe_close(mock_out)
        _safe_close(mock_err)
        


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensor-cmd", required=True,
                    help='Comando per avviare il Sensor, es: "py main.py --config ..\\..\\..\\exec\\windows\\config.ini"')
    ap.add_argument("--cwd", required=True, help="Working directory del Sensor (dove sta main.py)")
    ap.add_argument("--work-root", default=".", help="Root dove creare la cartella work (default: cartella corrente)")
    ap.add_argument("--base-port", type=int, default=5000)
    ap.add_argument("--only", nargs="*", help="Esegui solo questi scenari (es. --only S2 P1)")
    ap.add_argument("--all", action="store_true", help="Esegui tutti gli scenari")
    ap.add_argument("--config", default=None, help="File di config esterno (JSON o YAML)")
    ap.add_argument("--duration", type=float, default=12.0, help="Durata default per scenari time-based")
    ap.add_argument("--report-json", default=None, help="Salva report JSON")
    ap.add_argument("--html-report", default=None, help="Salva report HTML")
    ap.add_argument("--board-mock-cmd", default=None,
                    help='Comando per avviare il mock della board, es: "py ..\\..\\..\\scripts\\Sensor\\mock_udp_responder.py"')
    ap.add_argument("--board-mock-args", default="--uuid {uuid} --ip 127.0.0.1 --tcp-port 1105 --channels-map 11140000",
                    help="Argomenti del mock; {uuid} verrà sostituito con l'UUID del test")
    ap.add_argument("--sensor-wait-seconds", type=float, default=0.0, help="Attendi N secondi dopo che il Sensor è pronto e prima di lanciare lo stressor.")
    ap.add_argument("--stressor-extra", default="", help="Extra CLI to append to tcp_frame_stressor (e.g. \"--epoch-payload\")")
    
    args = ap.parse_args()

    # normalizza --only per accettare sia "P1 P2 A1" che "P1,P2,A1"
    only_raw = args.only or []
    only_list = []

    if isinstance(only_raw, str):
        only_list = [s.strip() for s in only_raw.replace(',', ' ').split() if s.strip()]
    else:
        # se nargs='*' o '+' può essere lista: supporta anche elementi con virgole
        for item in only_raw:
            only_list.extend(s.strip() for s in str(item).replace(',', ' ').split() if s.strip())

    args.only = only_list
    if args.only:
        print(f"[runner] Scenari richiesti: {', '.join(args.only)}", flush=True)

    cfg = load_config(args.config)
    scenario_order = cfg.get("scenario_order") or []
    sc_defs = cfg.get("scenarios") or {}

    if not args.all and not args.only:
        print("Usa --all oppure --only <SCENARI...>")
        return 2

    scenarios = scenario_order if args.all else args.only
    # normalizza work-root → …\Stress-Tests\work
    work_root = os.path.abspath(os.path.join(args.work_root, "work"))
    ensure_dir(work_root)

    try:
        results = []
        for sc in scenarios:
            if sc not in sc_defs:
                print(f"[WARN] Scenario {sc} non definito in config; skip")
                continue
            print(f"=== RUN {sc} ===")
            res = run_scenario(args.sensor_cmd, args.base_port, sc, sc_defs[sc],
                               cwd_sensor=args.cwd, work_root=work_root,
                               default_duration=args.duration,
                               sensor_wait_seconds=args.sensor_wait_seconds,
                               board_mock_cmd=args.board_mock_cmd,
                               board_mock_args=args.board_mock_args,
                               stressor_extra=args.stressor_extra)
                               
            results.append(res)
            print(json.dumps(res, indent=2))
            print()

        # sommario
        n_pass = sum(1 for r in results if r["status"] == "PASS")
        n_fail = sum(1 for r in results if r["status"] == "FAIL")
        n_err  = sum(1 for r in results if r["status"] == "ERROR")
        print(f"Summary: PASS={n_pass}  FAIL={n_fail}  ERROR={n_err} / {len(results)}")

        # salvataggi
        if args.report_json:
            ensure_dir(os.path.dirname(os.path.abspath(args.report_json)))
            with open(args.report_json, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
                
        pass
    except KeyboardInterrupt:
        print("\n[runner] Interrupted by user (Ctrl+C). Stopping.", flush=True)
        return 1        

    if args.html_report:
        html_report(os.path.abspath(args.html_report), results)

    return 0 if n_fail == 0 and n_err == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
