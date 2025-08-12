from modules.connections.sockets import SocketClient
from modules.connections.sockets import SocketServer
from modules.connections.http_client import Request
from modules.tools.log import log
from modules.packet import PacketHandler
import time, os, threading
from modules.sensor import Sensor

class Node:
    """Sensor node class"""

    def __init__(self, shutdown_event=None):
        self.running = False
        self.socket_server = None
        self.packet_handler = None
        self.shutdown_event = shutdown_event

        # watchdog “primo pacchetto”
        self._rx_watchdog = None
        self._rx_watchdog_stop = threading.Event()

        # supervisor/start lock
        self._start_lock = threading.Lock()
        self._supervisor_stop = threading.Event()
        self._supervisor_thr = None

        # Initialize the packet handler
        self.packet_handler: PacketHandler = PacketHandler(self.on_alarm_received)
        self.__create_socket_server()

        # avvia supervisore (configurabile)
        if os.getenv("SENSOR_SUPERVISOR_ENABLED", "1").lower() in ("1", "true", "yes"):
            self._start_supervisor()

    def start(self):
        """Start the sensor node"""
        if self.running:
            return
        # evita start concorrenti
        if not self._start_lock.acquire(blocking=False):
            return
        try:
            if not self.socket_server or not self.socket_server.running:
                self.on_error("Socket server is not running.")
                return

            log("[Node] Starting the sensor node...")
            self.__send_status(1, "Starting sensor")
            self.packet_handler.start()

            # 1. Configure the sensor node
            try:
                SocketClient.configure()
            except Exception as e:
                self.on_error(f"Error while configuring the sensor: {e}")
                self.packet_handler.stop()
                return

            time.sleep(1)

            # 2. Start sampling
            self.__send_status(3, "Sampling")
            log("[Node] Sensor node is about to run...")
            try:
                SocketClient.start_sampling()
            except Exception as e:
                self.on_error(f"Error while starting the sampling: {e}")
                self.__send_status(1, "Starting sensor")
                self.packet_handler.stop()
                return

            # Only here we can set the running flag to True
            # This ensures that the node is only marked as running after successful configuration and sampling start
            self.running = True
            self.__send_status(3, "Running")

            # 3a. abilita accept-watchdog SOLO dopo start(2)
            try:
                if self.socket_server:
                    self.socket_server.enable_accept_watchdog(True)
            except Exception:
                pass

            # 3b. Arm watchdog primo pacchetto (default derivato da WDT)
            wdt_env = os.getenv("SENSOR_BOARD_WDT") or os.getenv("SENSOR_BOARD_WDT_S") or "15"
            try:
                _wdt = float(wdt_env)
            except Exception:
                _wdt = 15.0
            fp_default = max(6.0, float(int(_wdt // 2)))
            first_to = float(os.getenv("SENSOR_FIRST_PACKET_TIMEOUT", str(fp_default)))
            if first_to > 0:
                self._start_rx_watchdog(first_to)

            log("[Node] Sensor node is running...")

        finally:
            try:
                self._start_lock.release()
            except Exception:
                pass

    def stop(self, msg: str = "Stopped"):
        """Stop the sensor node"""
        if not self.running:
            return

        log("[Node] Stopping the sensor node...")
        self.__send_status(2, msg)

        self.running = False
        self.packet_handler.stop()

        # disarma watchdog
        try:
            if self._rx_watchdog_stop:
                self._rx_watchdog_stop.set()
        except Exception:
            pass

        try:
            SocketClient.stop_sampling()
        except Exception as e:
            print(f"Error while stopping the sampling: {e}")
            
        # Disable accept-watchdog
        try:
            if self.socket_server:
                self.socket_server.enable_accept_watchdog(False)
        except Exception:
            pass

        # chiusura socket server
        try:
            if self.socket_server:
                # Disable eventual error callbacks to avoid restart during shutdown
                self.socket_server.on_error = None
                self.socket_server.close_connection()
                self.socket_server.close()
        except Exception as e:
            print(f"Error while closing the socket server: {e}")

        time.sleep(0.05)

    def restart(self):
        """Restart the sensor node"""
        log("[Node] Restarting the sensor node...")
        self.packet_handler.stop()
        self.running = False
        self.__send_status(2, "Restarting")

        try:
            if self.socket_server:
                self.socket_server.close_connection()
        except Exception as e:
            print(f"Error while closing the socket server: {e}")

        time.sleep(5)

        try:
            SocketClient.reset()
        except Exception as e:
            print(f"Error while resetting the sensor: {e}")

        time.sleep(10)
        self.start()
    
    def on_error(self, msg: str):
        """Handle the error"""
        log(f"[Node] Error: {msg}", "ERROR")

        # parametri comuni per auto-retry
        wdt_env = os.getenv("SENSOR_BOARD_WDT") or os.getenv("SENSOR_BOARD_WDT_S") or "15"
        try:
            wdt_s = float(wdt_env)
        except Exception:
            wdt_s = 15.0
        auto_restart = os.getenv("SENSOR_AUTO_RESTART_ON_TIMEOUT", "1").lower() in ("1", "true", "yes")
        backoff_after_reset = max(1.0, wdt_s + 2.0)  # rispetta WDT e aggiunge margine

        # First-packet timeout
        if isinstance(msg, str) and msg.lower().startswith("first-packet timeout"):
            try:
                SocketClient.stop_sampling()
                time.sleep(0.2)
                SocketClient.reset()
                log("[Node] Board stop+reset requested due to first-packet timeout", "INFO")
                if auto_restart and not (self.shutdown_event and self.shutdown_event.is_set()):
                    self.running = False
                    time.sleep(backoff_after_reset)
                    self.start()
                return
            except Exception as e:
                log(f"[Node] Watchdog recovery failed: {e}", "ERROR")

        # Timeout RX (connessione viva ma muta)
        if isinstance(msg, str) and msg.lower().startswith("timeout while reading"):
            try:
                SocketClient.stop_sampling()
                time.sleep(0.2)
                SocketClient.reset()
                log("[Node] Sensor board stop+reset due to RX timeout.", "INFO")
                if auto_restart and not (self.shutdown_event and self.shutdown_event.is_set()):
                    self.running = False
                    time.sleep(backoff_after_reset)
                    self.start()
                return
            except Exception as e:
                log(f"[Node] Recovery on timeout failed: {e}", "ERROR")

        # Accept-timeout (dopo '2', nessuna connessione)
        if isinstance(msg, str) and msg.lower().startswith("accept timeout"):
            try:
                SocketClient.stop_sampling()
                time.sleep(0.2)
                SocketClient.reset()
                log("[Node] Board stop+reset due to accept-timeout (no client).", "INFO")
                if auto_restart and not (self.shutdown_event and self.shutdown_event.is_set()):
                    self.running = False
                    time.sleep(backoff_after_reset)
                    self.start()
                return
            except Exception as e:
                log(f"[Node] Recovery on accept-timeout failed: {e}", "ERROR")

        # altri errori → riavvio classico, se non in shutdown
        if not (self.shutdown_event and self.shutdown_event.is_set()):
            log("[Node] Restarting the sensor node...")
            self.restart()
        else:
            log("[Node] Skip restart: shutting down", "DEBUG")

    def on_data_received(self, data: bytes, tStamp: int = None):
        """Handle the data received from the socket server"""
        try:
            # al primo pacchetto, disarma il watchdog
            try:
                self._rx_watchdog_stop.set()
            except Exception:
                pass
            self.packet_handler.add_packet(data, tStamp)
        except Exception as e:
            log(f"[NODE] on_data_received: some exception: {e}; len(data) = {len(data)}")

    def on_alarm_received(self, timestamp: int, status: int, data: list = None):
        """Handle the alarm received from the socket server"""
        if not data:
            self.__send_alarm(timestamp, status)
        else:
            start_timestamp = data[0][0]
            event_data = [item[1] for item in data]
            self.__send_alarm_data(timestamp, start_timestamp, event_data)

    def _start_rx_watchdog(self, first_timeout_s: float):
        """Start a one-shot watchdog: if no data arrives within first_timeout_s, stop+reset the board."""
        # kill previous watchdog if any
        try:
            self._rx_watchdog_stop.set()
        except Exception:
            pass
        self._rx_watchdog_stop = threading.Event()

        def _run():
            deadline = time.time() + first_timeout_s
            while not self._rx_watchdog_stop.is_set() and not (self.shutdown_event and self.shutdown_event.is_set()):
                if time.time() >= deadline:
                    log(f"[Node] First-packet timeout ({first_timeout_s}s) — no data received", "WARNING")
                    try:
                        SocketClient.stop_sampling()
                        time.sleep(0.2)
                        SocketClient.reset()
                        log("[Node] Board stop+reset requested due to first-packet timeout", "INFO")
                    except Exception as e:
                        log(f"[Node] Watchdog recovery failed: {e}", "ERROR")
                    return
                time.sleep(0.2)

        self._rx_watchdog = threading.Thread(target=_run, name="RxFirstPacketWatchdog", daemon=True)
        self._rx_watchdog.start()

    def _start_supervisor(self):
        """Supervisor: se il nodo non è running, prova a farlo ripartire con backoff."""
        def _run():
            base = 1.0
            while not self._supervisor_stop.is_set():
                if not self.running and not (self.shutdown_event and self.shutdown_event.is_set()):
                    if self._start_lock.acquire(blocking=False):
                        try:
                            self.start()
                            base = 1.0  # reset backoff se parte
                        finally:
                            try:
                                self._start_lock.release()
                            except Exception:
                                pass
                    # se non parte subito, exponential backoff
                    if not self.running:
                        time.sleep(base)
                        base = min(base * 2.0, 30.0)
                else:
                    time.sleep(1.0)

        self._supervisor_thr = threading.Thread(target=_run, name="NodeSupervisor", daemon=True)
        self._supervisor_thr.start()

    def __create_socket_server(self):
        """Create the socket server"""
        backoff = 1.0
        while True:
            try:
                log("[Node] Creating socket server")
                self.socket_server = SocketServer()
                self.socket_server.on_data_received = self.on_data_received
                self.socket_server.on_error = self.on_error
                self.socket_server.open()
                time.sleep(5)
                return
            except Exception as e:
                log(f"[Node] Error while creating socket server: {e}", "ERROR")
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2.0, 30)

    def __send_status(self, status: int, msg: str):
        """Send the sensor status"""
        try:
            payload = {
                "procStatus": status,
                "message": msg,
                "uuid": Sensor.get("uuid")
            }
            Request.update_sensor_status(payload)
        except Exception as e:
            log(f"[Node] Error while updating sensor status: {e}", "ERROR")

    def __send_alarm(self, timestamp: int, status: int):
        """Send the alarm status"""
        try:
            Request.update_alert(timestamp, status, Sensor.get("uuid"))
        except Exception as e:
            log(f"[Node] Error while updating alerting: {e}", "ERROR")

    def __send_alarm_data(self, trigger_timestamp: int, data_timestamp: int, data: list):
        """Send the alarm data"""
        try:
            Request.update_alerting_data(trigger_timestamp, data_timestamp, data, Sensor.get("uuid"))
        except Exception as e:
            log(f"[Node] Error while updating alerting data: {e}", "ERROR")