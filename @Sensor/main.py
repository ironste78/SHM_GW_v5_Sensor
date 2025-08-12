# @Sensor/main.py
# Main entry point for the Sensor backend service.
# Handles command-line arguments, initializes logging, and starts the Node.
import argparse
import os, time, json, signal, threading, sys
import errno
import platform
import ctypes  # ok anche su Linux: non fallisce l’import

# --- Single-instance helpers ---
def _is_process_running(pid: int) -> bool:
    """Ritorna True se il PID esiste ancora (best-effort cross-platform)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            # Se non riusciamo a verificare, meglio essere conservativi
            return True
    else:
        try:
            # Segnale 0: non uccide, ma valida l’esistenza del processo/permessi
            os.kill(int(pid), 0)
            return True
        except OSError as e:
            return e.errno != errno.ESRCH  # ESRC H= no such process ⇒ non vivo

def _ensure_single_instance(uuid: str, run_dir: str = None) -> str:
    """
    Crea un lock file atomico per UUID. Se esiste ed è vivo, esce con code=1.
    Se è stale (PID non esiste), lo rimuove e prosegue. Ritorna il path del lock.
    """
    if not uuid:
        # verrà comunque gestito più avanti dal parser (required)
        return None
    run_dir = run_dir or os.getenv("RUN_DIR", "./run")
    os.makedirs(run_dir, exist_ok=True)
    lock_path = os.path.join(run_dir, f"sensor_{uuid}.pid")

    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                old_pid_str = (f.read() or "").strip()
            old_pid = int(old_pid_str) if old_pid_str.isdigit() else -1
        except Exception:
            old_pid = -1

        if old_pid > 0 and _is_process_running(old_pid):
            print(f"[Main] Another Sensor with uuid={uuid} is already running (pid={old_pid}).", file=sys.stderr)
            sys.exit(1)
        else:
            # lock stale
            try:
                os.remove(lock_path)
            except Exception:
                pass

    # Creazione atomica
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)
    return lock_path

def _remove_lock(lock_path: str):
    if not lock_path:
        return
    try:
        os.remove(lock_path)
    except Exception:
        pass


# Optional setproctitle
try:
    import setproctitle
    HAVE_SETPROC = True
except Exception:
    HAVE_SETPROC = False

# === Phase 1: parse only --config AND --sensor-uuid (per logger) ===
pre = argparse.ArgumentParser(add_help=False)
pre.add_argument("--config", default="config.ini", help="Percorso al file di configurazione")
pre.add_argument("-su", "--sensor-uuid", help="UUID of the sensor (pre-parse for logger)", required=False)
pre_args, remaining_argv = pre.parse_known_args()

# UUID in env il prima possibile (se già passato da CLI)
if pre_args.sensor_uuid:
    os.environ["SENSOR_UUID"] = pre_args.sensor_uuid

_LOCK_PATH = _ensure_single_instance(os.getenv("SENSOR_UUID"))


# === Load config to environment ===
from modules.tools.config_loader import init_environment
env_tuple = init_environment(pre_args.config, show_env=True)
env = env_tuple[0] if isinstance(env_tuple, tuple) else env_tuple

# SOLO ora importo il logger: userà SENSOR_UUID per nominare il file
from modules.tools.log import log

# === Phase 2: full parser (defaults from env set by config) ===
parser = argparse.ArgumentParser(description="SENSOR Backend Service", parents=[pre])
# @Sensor
#parser.add_argument('-su', '--sensor-uuid', type=str, required=True, help='UUID of the sensor')
parser.add_argument('-sp', '--sensor-port', type=int, required=True, help='Port of the server socket application')
parser.add_argument('-sh', '--sensor-host', default=os.getenv('SENSOR_HOST', 'localhost'), help='Host of the server socket application')
parser.add_argument('-sf', '--sensor-data-filtered', type=int, default=int(os.getenv('SENSOR_DATA_FILTERED', 0)), help='Data filtered or data raw (1 or 0)')
parser.add_argument('-bip', '--board-ip', default=os.getenv('SENSOR_BOARD_IP', None), help='IP of the sensor board')
parser.add_argument('-bp', '--board-port', type=int, default=int(os.getenv('SENSOR_BOARD_PORT', 0)), help='Port of the sensor board')
parser.add_argument('-us', '--update-sensor', type=int, default=int(os.getenv('SENSOR_UPDATE_SENSOR', 1)), help='Update sensor')
# Dirs
parser.add_argument('-d', '--data-dir', default=os.getenv('DATA_DIR', '../data'), help='Data directory')
parser.add_argument('-l', '--log-dir', default=os.getenv('LOG_DIR', '../logs'), help='Log directory')
# Manager
parser.add_argument('-mh', '--manager-host', default=os.getenv('MANAGER_HOST', 'localhost'), help='Host of the sensor manager service')
parser.add_argument('-mp', '--manager-port', type=int, default=int(os.getenv('MANAGER_PORT', 7010)), help='Port of the sensor manager service')
# Database
parser.add_argument('-dh', '--database-host', default=os.getenv('DATABASE_HOST', 'localhost'), help='Host of the database service')
parser.add_argument('-dp', '--database-port', type=int, default=int(os.getenv('DATABASE_PORT', 7001)), help='Port of the database service')
# For stress test
parser.add_argument("--allow-unregistered", action="store_true",
    help="Avvia comunque il Sensor senza attendere la registrazione nel database (solo per test)")

args = parser.parse_args(remaining_argv)

# Export to env for downstream modules
uuid = (getattr(args, "sensor_uuid", None) 
        or os.getenv("SENSOR_UUID"))

if not uuid:
    # scegli tu se preferisci parser.error(...) o raise ValueError(...)
    raise ValueError("UUID is not set. Passa -su/--sensor-uuid oppure valorizza SENSOR_UUID.")

skip_db_wait = bool(os.getenv("SENSOR_ALLOW_UNREGISTERED") == "1" or getattr(args, "allow-unregistered", False))
if skip_db_wait:
    log("[Main] Test mode enabled: proceeding without database registration.", "WARNING")
    # Set for safety and then reuse in other functions
    os.environ["ALLOW_UNREGISTERED"] = "1"

os.environ["SENSOR_UUID"] = uuid
os.environ['SENSOR_HOST'] = args.sensor_host
os.environ['SENSOR_PORT'] = str(args.sensor_port)
os.environ['SENSOR_DATA_FILTERED'] = str(args.sensor_data_filtered)
if args.board_ip:
    os.environ['SENSOR_BOARD_IP'] = str(args.board_ip)
else:
    if os.getenv('SENSOR_BOARD_IP') in (None, '', 'None'):
        os.environ['SENSOR_BOARD_IP'] = '127.0.0.1'

if args.board_port and int(args.board_port) > 0:
    os.environ['SENSOR_BOARD_PORT'] = str(int(args.board_port))
else:
    # Se non è già definita, usa 1105 (porta console mock/board)
    if os.getenv('SENSOR_BOARD_PORT') in (None, '', '0'):
        os.environ['SENSOR_BOARD_PORT'] = '1105'

os.environ['SENSOR_UPDATE_SENSOR'] = str(args.update_sensor)
os.environ['DATA_DIR'] = args.data_dir
os.environ['LOG_DIR'] = args.log_dir
os.environ['MANAGER_HOST'] = args.manager_host
os.environ['MANAGER_PORT'] = str(args.manager_port)
os.environ['DATABASE_HOST'] = args.database_host
os.environ['DATABASE_PORT'] = str(args.database_port)


if HAVE_SETPROC:
    try:
        short = (args.sensor_uuid or os.getenv("SENSOR_UUID", ""))[:8]
        setproctitle.setproctitle(f'{os.getenv("SENSOR_PROC_NAME", "Sensor")}-{short}')
    except Exception:
        pass


from modules.connections.http_client import Request
from modules.connections.sockets import SocketClient
from modules.node import Node
from modules.sensor import Sensor

# === Graceful shutdown infrastructure ===
SHUTDOWN = threading.Event()
SIGNAL_HANDLED = threading.Event()
_node = None

def _signal_handler(sig, frame):
    if not SIGNAL_HANDLED.is_set():
        SIGNAL_HANDLED.set()
        SHUTDOWN.set()
        sig_name = {getattr(signal, n): n for n in dir(signal) if n.startswith('SIG')}.get(sig, str(sig))
        log(f"[Main] Caught signal {sig_name}. Shutting down gracefully...", "INFO")
        try:
            if _node is not None:
                _node.stop()
        except Exception as e:
            log(f"[Main] Error during node.stop(): {e}", "ERROR")
        try:
            _remove_lock(_LOCK_PATH)
        except Exception:
            pass
        

def _install_signal_handlers():
    # Always handle Ctrl+C (SIGINT)
    try:
        signal.signal(signal.SIGINT, _signal_handler)
    except Exception:
        pass
    # Handle SIGTERM (service stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except Exception:
            pass
    # Windows: Ctrl+Break
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _signal_handler)
        except Exception:
            pass

def enabling_sensor():
    global _node
    backoff         = 1.0
    # Limits for waiting data ready in the DB
    poll_interval   = float(os.getenv("SENSOR_DB_POLL_INTERVAL", 2.0)) # seconds
    max_wait        = float(os.getenv("SENSOR_DB_MAXWAIT", 120.0)) # seconds, 0=infinite
    waited          = 0.0
    while not SHUTDOWN.is_set():
        try:
            SENSOR_BOARD_IP = os.getenv('SENSOR_BOARD_IP', None)
            if (not skip_db_wait) and (SENSOR_BOARD_IP in (None, "None", "")):
                # 1. Try reading the sensor from the DB (it might not be there yet)
                data = Request.get_sensor()
                if data is None:
                    msg = (f"[Main] Sensor uuid={os.getenv('SENSOR_UUID')} not found in the database; "
                           f"waiting discovery registration (waited={waited:.1f}s, poll={poll_interval}s)")
                    log(msg, "INFO")
                    if max_wait > 0 and waited >= max_wait:
                        log(f"[Main] Max wait time exceeded ({max_wait}s). Will keep polling with backoff...", "WARNING")
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue

                # 2. Normalize and set environments
                Sensor.set(data)
                ip_host = Sensor.get("ipHost")
                ip_port = Sensor.get("ipPort")
                if isinstance(ip_host, dict):
                    ip_port = ip_host.get("port", ip_port)
                    ip_host = ip_host.get("host")
                os.environ['SENSOR_BOARD_IP']   = str(ip_host)
                os.environ['SENSOR_BOARD_PORT'] = str(0 if ip_port is None else ip_port)
                Request.update_sensor_status({ "procStatus": 1, "message": "Enabling sensor" })
            else:
                # (TEST) — opzionale: un messaggio di warning chiaro
                if skip_db_wait and SENSOR_BOARD_IP in (None, "None", ""):
                    log("[Main] Skipping database registration wait (test mode: allow-unregistered)", "WARNING")

                # Query current socket info and normalize keys
                sensor_info = [json.loads(SocketClient.info())]
                new_sensor_info = []
                for mdict in sensor_info:
                    new_dict = { k.lower(): v for k, v in mdict.items() }
                    new_sensor_info.append(new_dict)
                Sensor.set(new_sensor_info[0])

            log(f"[Main] Starting node for uuid={os.getenv('SENSOR_UUID')} at {os.getenv('SENSOR_HOST')}:{os.getenv('SENSOR_PORT')}", "INFO")
            _node = Node(shutdown_event=SHUTDOWN)
            _node.start()
            log("[Main] Node started. Waiting for shutdown signal...", "INFO")
            return
        except Exception as e:
            log(f"[Main] Error while connecting to the sensor: {e}", "ERROR")
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)

def main():
    _install_signal_handlers()
    enabling_sensor()
    # Idle loop until a signal arrives
    try:
        while not SHUTDOWN.is_set():
            time.sleep(0.2)
    finally:
        try:
            if _node is not None:
                _node.stop()
        except Exception as e:
            log(f"[Main] Error on shutdown: {e}", "ERROR")
        try:
            _remove_lock(_LOCK_PATH)
        except Exception:
            pass

        log("[Main] Shutdown complete.", "INFO")

if __name__ == '__main__':
    main()
