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
from datetime import datetime, timezone
from html import escape


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

def html_report(out_path: str, final_report: dict) -> None:
    """
    Renderizza un report HTML a partire dal dizionario 'final_report' che contiene:
      - collected_at_utc: str
      - repo_url, commit_sha, commit_short, branch, dirty
      - summary: {pass, fail, error, total}
      - results: [ {scenario, name, uuid, status, log, checks:[...], reason?, mock_error_tail?, stressor_error_tail?, ...}, ... ]
    """
    def _fmt_bool(b):
        return "yes" if b else "no"

    def _status_class(s):
        s = (s or "").upper()
        return {"PASS": "ok", "FAIL": "fail", "ERROR": "error"}.get(s, "unknown")

    def _linkify(path):
        if not path:
            return ""
        # se è un path esistente, rende un link file://, altrimenti testo plain
        href = path
        return f'<a href="file:///{escape(href.replace("\\\\", "/"))}" title="{escape(path)}">{escape(os.path.basename(path))}</a>'

    meta = final_report or {}
    results = meta.get("results", [])
    summary = meta.get("summary", {})
    collected_at = meta.get("collected_at_utc", "")
    repo_url = meta.get("repo_url", "")
    commit_sha = meta.get("commit_sha", "")
    commit_short = meta.get("commit_short", "")
    branch = meta.get("branch", "")
    dirty = bool(meta.get("dirty", False))

    # link commit se repo_url è GitHub-like
    if repo_url and commit_sha and ("github.com" in repo_url.lower()):
        commit_html = f'<a href="{escape(repo_url)}/commit/{escape(commit_sha)}" target="_blank">{escape(commit_short or commit_sha[:12])}</a>'
    else:
        commit_html = escape(commit_short or (commit_sha[:12] if commit_sha else ""))

    rows = []
    for r in results:
        scen   = r.get("scenario", "")
        name   = r.get("name", "")
        uuid   = r.get("uuid", "")
        status = r.get("status", "")
        logp   = r.get("log", "")
        reason = r.get("reason", "")
        st_tail = r.get("stressor_error_tail", "")
        mk_tail = r.get("mock_error_tail", "")
        checks = r.get("checks", []) or []

        # dettagli
        details_lines = []
        if reason:
            details_lines.append(f'<div class="reason"><b>Reason:</b> {escape(reason)}</div>')

        # tail (stderr)
        if st_tail:
            details_lines.append('<div class="tail"><b>Stressor stderr (tail):</b><pre>' + escape(st_tail) + '</pre></div>')
        if mk_tail:
            details_lines.append('<div class="tail"><b>Mock stderr (tail):</b><pre>' + escape(mk_tail) + '</pre></div>')

        # comandi (se salvati nel risultato)
        if r.get("sensor_cmd"):
            details_lines.append(f'<div class="cmd"><b>Sensor cmd:</b> <code>{escape(r["sensor_cmd"])}</code></div>')
        if r.get("mock_cmd"):
            details_lines.append(f'<div class="cmd"><b>Mock cmd:</b> <code>{escape(r["mock_cmd"])}</code></div>')
        if r.get("stressor_cmd"):
            details_lines.append(f'<div class="cmd"><b>Stressor cmd:</b> <code>{escape(r["stressor_cmd"])}</code></div>')

        # args stressor (se presenti)
        if isinstance(r.get("stressor_args"), dict):
            details_lines.append('<div class="args"><b>Stressor args:</b> <pre>' +
                                 escape(json.dumps(r["stressor_args"], indent=2)) + '</pre></div>')

        # checks
        if checks:
            ch_html = ['<div class="checks"><b>Checks:</b><ul>']
            for c in checks:
                pat = c.get("pattern", "")
                cnt = c.get("count", 0)
                matched = c.get("matched", None)
                minv = c.get("min", None)
                maxv = c.get("max", None)
                cls = ""
                if matched is False:
                    cls = ' class="bad"'
                ch_html.append(f'<li{cls}><code>{escape(pat)}</code> → count={cnt}'
                               + (f", min={minv}" if minv is not None else "")
                               + (f", max={maxv}" if maxv is not None else "")
                               + (", matched=✗" if matched is False else (", matched=✓" if matched is True else ""))
                               + '</li>')
            ch_html.append("</ul></div>")
            details_lines.append("".join(ch_html))

        # link al log
        log_html = _linkify(logp)

        rows.append(f"""
        <tr class="{_status_class(status)}">
          <td class="scenario">{escape(str(scen))}</td>
          <td class="name">{escape(name)}</td>
          <td class="uuid">{escape(uuid)}</td>
          <td class="status">{escape(status)}</td>
          <td class="log">{log_html}</td>
          <td class="details">{''.join(details_lines) or ''}</td>
        </tr>
        """)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Sensor Stress Report</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 20px; }}
h1 {{ margin: 0 0 8px 0; }}
.meta {{ margin: 8px 0 16px 0; font-size: 14px; color: #333; }}
.meta code {{ background:#f6f8fa; padding:2px 4px; border-radius:4px; }}
.summary {{ margin: 12px 0 16px 0; padding: 8px; background:#f6f8fa; border:1px solid #e1e4e8; border-radius:6px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
th {{ background: #fafafa; text-align: left; }}
tr.ok td.status {{ color: #0b7a0b; font-weight: 700; }}
tr.fail td.status {{ color: #ad2c24; font-weight: 700; }}
tr.error td.status {{ color: #9f36c2; font-weight: 700; }}
tr.unknown td.status {{ color: #666; font-weight: 700; }}
td.details .tail pre, td.details .args pre {{ background:#f6f8fa; border:1px solid #e1e4e8; border-radius:6px; padding:8px; white-space: pre-wrap; }}
td.details .checks ul {{ margin:6px 0 0 18px; }}
td.details .checks li.bad {{ color:#ad2c24; font-weight:600; }}
.small {{ font-size: 12px; color:#555; }}
.footer {{ margin-top: 22px; font-size: 12px; color:#666; }}
</style>
</head>
<body>
  <h1>Sensor Stress Report</h1>
  <div class="meta">
    <div><b>Collected at (UTC):</b> {escape(collected_at)}</div>
    <div><b>Repo:</b> {('<a href="' + escape(repo_url) + '" target="_blank">' + escape(repo_url) + '</a>') if repo_url else '-'}</div>
    <div><b>Commit:</b> {commit_html or '-'} &nbsp; <span class="small">(full: {escape(commit_sha) if commit_sha else '-'})</span></div>
    <div><b>Branch:</b> {escape(branch) if branch else '-'} &nbsp;&nbsp; <b>Dirty:</b> {_fmt_bool(dirty)}</div>
  </div>

  <div class="summary">
    <b>Summary:</b> PASS={summary.get('pass', 0)} &nbsp; FAIL={summary.get('fail', 0)} &nbsp; ERROR={summary.get('error', 0)} &nbsp; / {summary.get('total', len(results))}
  </div>

  <table>
    <thead>
      <tr>
        <th>Scenario</th>
        <th>Name</th>
        <th>UUID</th>
        <th>Status</th>
        <th>Sensor log</th>
        <th>Details</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="6" class="small">No results.</td></tr>'}
    </tbody>
  </table>

  <div class="footer">Generated on {escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# -------------------- Core --------------------
def run_scenario(sensor_cmd: str, port: int, scenario: str, sc_def: Dict[str, Any],
                 cwd_sensor: str, work_root: str, default_duration: float = 12.0, sensor_wait_seconds: float = 0.0,
                 board_mock_cmd: Optional[str] = None, board_mock_args: Optional[str] = None, stressor_extra: str = "") -> Dict[str, Any]:
    sensor = stress = mock_proc = None
    sensor_out = sensor_err = mock_out = mock_err = None

    sid = f"{scenario}-{uuid.uuid4().hex[:8]}"
    uuid_str = f"test-{sid}"
    work_dir = os.path.abspath(os.path.join(work_root, f"{sid}"))
    log_dir = os.path.join(work_dir, "logs")
    ensure_dir(log_dir)

    # percorsi log
    sensor_out_path = os.path.join(work_dir, "sensor_stdout.log")
    sensor_err_path = os.path.join(work_dir, "sensor_stderr.log")
    mock_out_path   = os.path.join(work_dir, "mock_stdout.log")
    mock_err_path   = os.path.join(work_dir, "mock_stderr.log")
    st_out_path     = os.path.join(work_dir, "stressor_stdout.log")
    st_err_path     = os.path.join(work_dir, "stressor_stderr.log")

    # metadati repo (se presenti nell’ambiente)
    provenance = {
        "repo_url":   os.environ.get("GIT_URL"),
        "repo_branch":os.environ.get("GIT_BRANCH"),
        "repo_commit":os.environ.get("GIT_COMMIT"),
        "runner_sha": os.environ.get("RUNNER_SHA"),
    }

    # precompilo alcune stringhe che riporteremo nel risultato
    mock_cmd_effective = None
    sensor_cmd_effective = None
    stressor_cmd_printable = None

    try:
        # --- 1) Avvio mock della board (console TCP) se richiesto
        console_port = parse_console_port(board_mock_args or "")
        if board_mock_cmd:
            mock_cmd_effective = f'{board_mock_cmd} {(board_mock_args or "").format(uuid=uuid_str)}'
            mock_env = os.environ.copy()
            mock_out = open(mock_out_path, "wb")
            mock_err = open(mock_err_path, "wb")
            print_step(f"Starting board mock: {mock_cmd_effective}")
            creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            mock_proc = subprocess.Popen(
                mock_cmd_effective, cwd=HERE, env=mock_env, shell=True,
                stdout=mock_out, stderr=mock_err,
                creationflags=creationflags
            )

            print_step(f"Waiting board mock on tcp://127.0.0.1:{console_port} ...")
            if not wait_port("127.0.0.1", console_port, timeout=15.0):
                tail = _read_tail(mock_err_path, max_lines=10)
                kill_proc(mock_proc)
                return {
                    "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR",
                    "reason": f"Board mock not listening on {console_port}",
                    "log": mock_out_path,
                    "mock_error_tail": tail,
                    "board_mock_cmd": mock_cmd_effective,
                    "provenance": provenance,
                    "logs": {
                        "mock_stdout": mock_out_path,
                        "mock_stderr": mock_err_path
                    }
                }

        # --- 2) Avvio Sensor
        env = os.environ.copy()
        env["LOG_DIR"] = log_dir
        env["RUN_DIR"] = os.path.join(work_dir, "run")
        env["SENSOR_ALLOW_UNREGISTERED"] = "1"
        ensure_dir(env["RUN_DIR"])

        sensor_out = open(sensor_out_path, "wb")
        sensor_err = open(sensor_err_path, "wb")

        sensor_cmd_effective = f'{sensor_cmd} -su {uuid_str} -sp {port} -sh 127.0.0.1'
        print_step(f"Starting Sensor: (cwd={cwd_sensor}) {sensor_cmd_effective}")
        sensor = subprocess.Popen(
            sensor_cmd_effective, cwd=cwd_sensor, env=env, shell=True,
            stdout=sensor_out, stderr=sensor_err,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
        )

        # fail-fast se il processo termina subito
        time.sleep(0.8)
        if sensor.poll() is not None:
            diag = ""
            try:
                diag = (tail_file(sensor_out_path) + "\n" + tail_file(sensor_err_path)).strip()
            except Exception:
                pass
            if mock_proc: kill_proc(mock_proc)
            return {
                "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                "status": "ERROR", "reason": "Sensor exited prematurely",
                "log": sensor_err_path, "diagnostic": diag,
                "sensor_cmd": sensor_cmd_effective,
                "provenance": provenance,
                "logs": {
                    "sensor_stdout": sensor_out_path,
                    "sensor_stderr": sensor_err_path,
                    "mock_stdout": mock_out_path if board_mock_cmd else None,
                    "mock_stderr": mock_err_path if board_mock_cmd else None
                }
            }

        print_step("Waiting for 'Socket server opened ...' marker in Sensor log ...")
        if not wait_server_opened(log_dir, port, timeout=30.0):
            kill_proc(sensor)
            if mock_proc: kill_proc(mock_proc)
            return {
                "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                "status": "ERROR", "reason": "Sensor did not open server (no log marker)",
                "log": find_sensor_log(log_dir) or sensor_err_path,
                "sensor_cmd": sensor_cmd_effective,
                "provenance": provenance
            }
        t_open_seen = time.time()

        time.sleep(1.2)

        print_step(f"Checking tcp://127.0.0.1:{port} reachable ...")
        if not wait_port("127.0.0.1", port, timeout=5.0):
            try:
                if not wait_port("::1", port, timeout=2.0):
                    raise RuntimeError("no v6 either")
            except Exception:
                kill_proc(sensor)
                if mock_proc: kill_proc(mock_proc)
                return {
                    "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                    "status": "ERROR", "reason": "Sensor port not open",
                    "log": find_sensor_log(log_dir) or sensor_err_path,
                    "sensor_cmd": sensor_cmd_effective,
                    "provenance": provenance
                }

        print_step("Waiting for '[PacketHandler] Packet handler is running...' marker in Sensor log ...")
        if not wait_packet_handler_started(log_dir, timeout=30.0):
            kill_proc(sensor)
            if mock_proc: kill_proc(mock_proc)
            return {
                "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                "status": "ERROR", "reason": "Packet handler never started (no log marker)",
                "log": find_sensor_log(log_dir) or sensor_err_path,
                "sensor_cmd": sensor_cmd_effective,
                "provenance": provenance
            }

        # clamp del wait in base a accept-timeout
        if sensor_wait_seconds and sensor_wait_seconds > 0:
            accept_to = int(os.environ.get("SENSOR_ACCEPT_TIMEOUT", "20"))
            elapsed = time.time() - t_open_seen
            margin  = 5
            budget  = max(0, accept_to - margin - elapsed)
            eff_wait = min(sensor_wait_seconds, budget)
            print_step(f"Sleeping {eff_wait:.0f}s "
                       f"(elapsed since server-open={elapsed:.1f}s, "
                       f"accept-timeout={accept_to}s, margin={margin}s) "
                       "before launching stressor ...")
            time.sleep(eff_wait)

        # --- 3) Esecuzione scenario (stressor)
        if sc_def.get("stressor"):
            st = sc_def["stressor"]
            st_scenario = st.get("scenario", scenario)
            st_args = st.get("args", {})
            if "duration" not in st_args and scenario in ("S2","S3","S5","A1"):
                st_args["duration"] = default_duration

            py = sys.executable or "python"
            args_list = ["--host", "127.0.0.1", "--port", str(port), "--scenario", st_scenario]

            def _dash(s: str) -> str:
                return s.replace("_", "-")

            for k, v in st_args.items():
                flag = f"--{_dash(str(k))}"
                if isinstance(v, bool):
                    if v:
                        args_list.append(flag)
                elif v is None or v == "":
                    continue
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        args_list += [flag, str(item)]
                else:
                    args_list += [flag, str(v)]

            extra_list = shlex.split(stressor_extra) if stressor_extra else []
            cmd_list = [py, STRESSOR] + args_list + extra_list

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
                try:
                    stressor_cmd_printable = shlex.join(cmd_list)
                except AttributeError:
                    stressor_cmd_printable = " ".join(cmd_list)
                print(f"[runner] Stressor started: {stressor_cmd_printable}")
                print(f"[runner] Stressor args map: {st_args}")

                timeout = float(sc_def.get("timeout", 0)) or (default_duration + 90)
                stress.wait(timeout=timeout)

                st_rc = stress.returncode if stress else None
                if st_rc not in (0, None):
                    result = {
                        "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                        "status": "ERROR",
                        "reason": f"Stressor exited with code {st_rc}",
                        "log": find_sensor_log(log_dir) or sensor_err_path,
                        "sensor_wait_seconds": sensor_wait_seconds,
                        "stressor_stderr": st_err_path,
                        "sensor_cmd": sensor_cmd_effective,
                        "board_mock_cmd": mock_cmd_effective,
                        "stressor_cmd": stressor_cmd_printable,
                        "stressor_args": st_args,
                        "stressor_extra": stressor_extra,
                        "provenance": provenance,
                        "logs": {
                            "sensor_stdout": sensor_out_path,
                            "sensor_stderr": sensor_err_path,
                            "mock_stdout": mock_out_path if board_mock_cmd else None,
                            "mock_stderr": mock_err_path if board_mock_cmd else None,
                            "stressor_stdout": st_out_path,
                            "stressor_stderr": st_err_path
                        }
                    }
                    # tail utile per HTML
                    tail = _read_tail(st_err_path, max_lines=3)
                    if tail:
                        result["stressor_error_tail"] = tail
                    kill_proc(sensor)
                    if mock_proc: kill_proc(mock_proc)
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
            print(f"[runner] No stressor configured for scenario {scenario} — skipping stressor launch")
            wait_s = float(sc_def.get("sensor_wait_seconds", 25))
            time.sleep(wait_s)

        # lascia drenare i log
        time.sleep(2.0)

        # --- 4) chiusura processi
        kill_proc(sensor)
        if mock_proc:
            kill_proc(mock_proc)

        # --- 5) verifica risultati
        log_path = find_sensor_log(log_dir)
        if not log_path:
            return {
                "scenario": scenario, "name": sc_def.get("name",""), "uuid": uuid_str,
                "status": "ERROR", "reason": "Log file not found",
                "sensor_cmd": sensor_cmd_effective,
                "board_mock_cmd": mock_cmd_effective,
                "stressor_cmd": stressor_cmd_printable,
                "provenance": provenance
            }

        content = tail_file(log_path)
        passed = True
        details = []
        for patt in sc_def.get("patterns", []):
            rx = patt.get("re") or patt.get("pattern")
            if not rx:
                continue
            minv = patt.get("min", None)
            maxv = patt.get("max", None)
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
            "sensor_cmd": sensor_cmd_effective,
            "board_mock_cmd": mock_cmd_effective,
            "stressor_cmd": stressor_cmd_printable,
            "provenance": provenance,
            "logs": {
                "sensor_stdout": sensor_out_path,
                "sensor_stderr": sensor_err_path,
                "mock_stdout": mock_out_path if board_mock_cmd else None,
                "mock_stderr": mock_err_path if board_mock_cmd else None,
                "stressor_stdout": st_out_path if sc_def.get("stressor") else None,
                "stressor_stderr": st_err_path if sc_def.get("stressor") else None
            }
        }

    finally:
        # cleanup robusto
        try: kill_proc(stress)
        except Exception: pass
        try: kill_proc(sensor)
        except Exception: pass
        try: kill_proc(mock_proc)
        except Exception: pass

        _safe_close(sensor_out)
        _safe_close(sensor_err)
        _safe_close(mock_out)
        _safe_close(mock_err)

        

def _git_cmd(cwd, *args):
    try:
        # Usare shlex.quote non serve con lista, ma lasciamo robusto
        out = subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""

def detect_git_info(cwd, prefer_given_url="", prefer_given_sha=""):
    """
    Rileva commit SHA/branch/dirty + remote URL dal repo git in `cwd`.
    I parametri prefer_given_* hanno priorità, se non vuoti.
    Ritorna un dict:
    {
      "repo_url": "...",
      "commit_sha": "...", "commit_short": "...",
      "branch": "...", "dirty": bool
    }
    """
    info = {
        "repo_url": prefer_given_url or "",
        "commit_sha": prefer_given_sha or "",
        "commit_short": "",
        "branch": "",
        "dirty": False,
    }

    # Se già passati da CLI, basta calcolare lo short
    if info["commit_sha"]:
        info["commit_short"] = info["commit_sha"][:12]
        return info

    # Prova a leggere da git (se disponibile)
    head = _git_cmd(cwd, "rev-parse", "HEAD")
    if head:
        info["commit_sha"] = head
        info["commit_short"] = _git_cmd(cwd, "rev-parse", "--short=12", "HEAD") or head[:12]
        info["branch"] = _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        info["dirty"] = bool(_git_cmd(cwd, "status", "--porcelain"))
        # Prova a prendere l’URL del remote
        if not info["repo_url"]:
            url = _git_cmd(cwd, "config", "--get", "remote.origin.url")
            info["repo_url"] = url or ""
        return info

    # Fallback “grezzo”: prova a leggere .git/HEAD (se git non c’è nel PATH)
    try:
        head_file = os.path.join(cwd, ".git", "HEAD")
        if os.path.isfile(head_file):
            with open(head_file, "r", encoding="utf-8", errors="ignore") as f:
                line = f.read().strip()
            if line.startswith("ref: "):
                ref = line.split(" ", 1)[1].strip()
                info["branch"] = ref.split("/")[-1]
                ref_file = os.path.join(cwd, ".git", ref.replace("/", os.sep))
                if os.path.isfile(ref_file):
                    with open(ref_file, "r", encoding="utf-8", errors="ignore") as f:
                        sha = f.read().strip()
                    info["commit_sha"] = sha
                    info["commit_short"] = sha[:12]
    except Exception:
        pass

    return info

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
    ap.add_argument("--auto-git", action="store_true", help="Auto-detect repo URL and commit SHA from local git")
    ap.add_argument("--append-sha-to-report", action="store_true", help="Append short SHA to report filenames")
    ap.add_argument("--repo-url",    default=os.getenv("REPO_URL", ""))
    ap.add_argument("--commit-sha",  default=os.getenv("COMMIT_SHA", ""))
    ap.add_argument("--branch",      default=os.getenv("BRANCH", ""))
    ap.add_argument("--dirty",       default=os.getenv("DIRTY", "0"), help="1/true se la working copy ha modifiche locali")

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

    # --- Repo/commit metadata
    repo_meta = {}
    if args.auto_git or args.repo_url or args.commit_sha:
        repo_meta = detect_git_info(
            cwd=args.cwd, 
            prefer_given_url=args.repo_url, 
            prefer_given_sha=args.commit_sha
        )
    else:
        repo_meta = {"repo_url":"", "commit_sha":"", "commit_short":"", "branch":"", "dirty": False}

    # Timestamp raccolta
    collected_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


    scenarios = scenario_order if args.all else args.only
    # normalizza work-root → …\Stress-Tests\work
    work_root = os.path.abspath(os.path.join(args.work_root, "work"))
    ensure_dir(work_root)

    try:
        # --- Repo/commit metadata (auto o espliciti da CLI/ENV)
        if (getattr(args, "auto_git", False) or args.repo_url or args.commit_sha) and "detect_git_info" in globals():
            repo_meta = detect_git_info(
                cwd=args.cwd,
                prefer_given_url=args.repo_url,
                prefer_given_sha=args.commit_sha
            )
        else:
            # fallback “best effort” se non c'è detect_git_info
            repo_meta = {
                "repo_url": getattr(args, "repo_url", ""),
                "commit_sha": getattr(args, "commit_sha", ""),
                "commit_short": (getattr(args, "commit_sha", "") or "")[:12],
                "branch": "",
                "dirty": False,
            }

        # opzionale: appende lo short SHA ai nomi file report
        if getattr(args, "append_sha_to_report", False) and repo_meta.get("commit_short"):
            short = repo_meta["commit_short"]
            def _append_sha(path: str) -> str:
                root, ext = os.path.splitext(os.path.abspath(path))
                return f"{root}_{short}{ext}"
            if args.report_json:
                args.report_json = _append_sha(args.report_json)
            if args.html_report:
                args.html_report = _append_sha(args.html_report)

        collected_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        results = []
        for sc in scenarios:
            if sc not in sc_defs:
                print(f"[WARN] Scenario {sc} non definito in config; skip")
                continue
            print(f"=== RUN {sc} ===")
            res = run_scenario(
                args.sensor_cmd, args.base_port, sc, sc_defs[sc],
                cwd_sensor=args.cwd, work_root=work_root,
                default_duration=args.duration,
                sensor_wait_seconds=args.sensor_wait_seconds,
                board_mock_cmd=args.board_mock_cmd,
                board_mock_args=args.board_mock_args,
                stressor_extra=args.stressor_extra
            )

            # Inietta i metadati anche nel risultato del singolo scenario
            res.update({
                "repo_url":   repo_meta.get("repo_url", ""),
                "commit_sha": repo_meta.get("commit_sha", ""),
                "commit_short": repo_meta.get("commit_short", ""),
                "branch": repo_meta.get("branch", ""),
                "dirty": bool(repo_meta.get("dirty", False)),
            })

            results.append(res)
            print(json.dumps(res, indent=2))
            print()

            # --- inject provenance into each test result
            res.setdefault("provenance", {})
            res["provenance"]["repo_url"] = args.repo_url or ""
            res["provenance"]["repo_branch"] = args.branch or ""
            res["provenance"]["repo_commit"] = args.commit_sha or ""
            res["provenance"]["runner_sha"] = ""  # opzionale, se vuoi firmare il runner stesso

            # flat fields (per comodità nel report)
            res["repo_url"] = args.repo_url or ""
            res["commit_sha"] = args.commit_sha or ""
            res["commit_short"] = (args.commit_sha[:8] if args.commit_sha else "")
            res["branch"] = args.branch or ""
            res["dirty"] = str(args.dirty).lower() not in ("0","false","no","")


        # sommario
        n_pass = sum(1 for r in results if r.get("status") == "PASS")
        n_fail = sum(1 for r in results if r.get("status") == "FAIL")
        n_err  = sum(1 for r in results if r.get("status") == "ERROR")
        print(f"Summary: PASS={n_pass}  FAIL={n_fail}  ERROR={n_err} / {len(results)}")

        summary_dict = {
            "pass": n_pass,
            "fail": n_fail,
            "error": n_err,
            "total": len(results),
        }

        # report finale con metadati + risultati
        final_report = {
            "collected_at_utc": collected_at_iso,
            "repo_url": repo_meta.get("repo_url", ""),
            "commit_sha": repo_meta.get("commit_sha", ""),
            "commit_short": repo_meta.get("commit_short", ""),
            "branch": repo_meta.get("branch", ""),
            "dirty": bool(repo_meta.get("dirty", False)),
            "summary": summary_dict,
            "results": results,
        }

        # salvataggi
        if args.report_json:
            ensure_dir(os.path.dirname(os.path.abspath(args.report_json)))
            meta = {
                "collected_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "repo_url": args.repo_url or "",
                "commit_sha": args.commit_sha or "",
                "commit_short": (args.commit_sha[:8] if args.commit_sha else ""),
                "branch": args.branch or "",
                "dirty": str(args.dirty).lower() not in ("0","false","no",""),
                "summary": { "pass": n_pass, "fail": n_fail, "error": n_err, "total": len(results) }
            }
            with open(args.report_json, "w", encoding="utf-8") as f:
                json.dump({**meta, "results": results}, f, indent=2)


    except KeyboardInterrupt:
        print("\n[runner] Interrupted by user (Ctrl+C). Stopping.", flush=True)
        return 1

    # HTML: passa l'oggetto completo (adatta la funzione se accettava solo 'results')
    if args.html_report:
        html_report(os.path.abspath(args.html_report), final_report)

    return 0 if n_fail == 0 and n_err == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
