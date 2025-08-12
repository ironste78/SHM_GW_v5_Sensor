from modules.sensor import Sensor
from modules.tools.log import log
from socket import socket, timeout, AF_INET, SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR, IPPROTO_TCP, TCP_NODELAY
from threading import Thread, current_thread
from modules.storer import AccelerometerStorer
from modules.connections.udp import UDPSender
import time
from datetime import datetime
import os
import struct

class SocketClient:
    """Socket client class to connect to the sensor and send commands."""
    
    SO_TIMESTAMPNS = 35

    @classmethod
    def info(cls):
        """Get the information from the sensor (command 1)."""
        def callable(socket):
            data = socket.recv(1024)
            while "SHM_console#" not in data.decode():
                data = socket.recv(1024)

            if "SHM_console#" in data.decode():
                log("[SocketClient] Sending command 1 to get information")
                socket.send("1".encode())
                data = socket.recv(1024)
                data = data.decode().strip()
                if "UUID" in data:
                    return data.split("SHM_console#")[0].replace("'", "\"")
                else:
                    raise ValueError("Error in the response")
            raise ValueError("Error in the response")
        return cls.__connect(callable)

    @classmethod
    def configure(cls):
        """Configure the sensor (command: c)."""
        def callable(socket: socket):
            host = os.getenv('SENSOR_HOST', '10.0.0.99')
            port = os.getenv('SENSOR_PORT', 5000)
            
            data = socket.recv(1024)
            while "SHM_console#" not in data.decode():
                data = socket.recv(1024)

            if "SHM_console#" in data.decode():
                log(f"[SocketClient] Configuring the sensor with: C {host}:{port}")
                socket.send(f"C {host}:{port}".encode())
                data = socket.recv(1024)
                if "[OK]" in data.decode():
                    log(f"[SocketClient] Sensor configured")
                    return
            raise ValueError("Error while configuring the sensor")
        cls.__connect(callable)

    @classmethod
    def start_sampling(cls):
        """Start the sampling (command: 2)."""
        def callable(socket):
            data = socket.recv(1024)
            while "SHM_console#" not in data.decode():
                data = socket.recv(1024)

            if "SHM_console#" in data.decode():
                log("[SocketClient] Starting the sampling")
                
                # Store the sending moment of 2!!
                tStamp = int(time.time() * 1000)

                #socket.send(f"M 1;tStamp={tStamp}".encode())
                if (int(os.getenv('SENSOR_HEADER_ONLY', 0)) == 1):
                    socket.sendall(f"2 1;tStamp={tStamp}".encode())
                else:
                    socket.sendall(f"2;tStamp={tStamp}".encode())

                AccelerometerStorer.set_start_sampling_timestamp(tStamp)

                data = socket.recv(1024)
                if "[OK]" in data.decode():
                    mac = Sensor.get('mac').replace(":", "")  # MAC address of the device
                    log(f"[SocketClient] Sampling started - MAC: {mac} -- Tstamp: {tStamp}")
                    return
                elif "[KO]" in data.decode():
                    raise ValueError("Something went wrong or just the sensor is already sampling")
            raise ValueError("Error while starting the sampling")
        cls.__connect(callable)

    @classmethod
    def stop_sampling(cls):
        """Stop the sampling (command: 3)."""
        def callable(socket):
            data = socket.recv(1024)
            while "SHM_console#" not in data.decode():
                data = socket.recv(1024)

            if "SHM_console#" in data.decode():
                log("[SocketClient] Stopping the sampling")
                socket.send("3".encode())
                data = socket.recv(1024)
                if "[OK]" in data.decode():
                    return
                elif "[KO]" in data.decode():
                    raise ValueError("Sensor is already stopped")
            raise ValueError("Error while stopping the sampling")
        cls.__connect(callable)

    @classmethod
    def reset(cls):
        """Reset the sensor (command: 7 1)."""
        def callable(socket):
            data = socket.recv(1024)
            while "SHM_console#" not in data.decode():
                data = socket.recv(1024)

            if "SHM_console#" in data.decode():
                log("[SocketClient] Resetting the sensor")
                socket.send("7 1".encode())
        cls.__connect(callable)

    @classmethod
    def __connect(cls, callable: callable):
        """Connect to the sensor."""
        client: socket = None
        ret = None
        try:
            host = os.getenv('SENSOR_BOARD_IP', 'localhost')
            port = int(os.getenv('SENSOR_BOARD_PORT', 1105))

            if not port or port <= 0:
                port = 1105  # fallback robusto

            client: socket = socket(AF_INET, SOCK_STREAM)
            try:
                client.setsockopt(SOL_SOCKET, cls.SO_TIMESTAMPNS, 1)
            except Exception as e:
                log(f"[SocketClient] SO_TIMESTAMPNS not supported, proceeding without kernel timestamp", "Debug")

            client.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)

            client.settimeout(5)
            client.connect((host, port))
            log(f"[SocketClient] Connected to the sensor at {host}:{port}")
            if callable:
                ret = callable(client)

            if client:
                client.send("Q".encode())
            return ret
        except timeout:
            raise ValueError("[SocketClient] Error in socket client: Connection timeout")
        except Exception as e:
            raise ValueError(f"[SocketClient] Error in socket client: {e}")
        finally:
            if client:
                client.close()
                log(f"[SocketClient] Disconnected from the sensor at {host}:{port}")
        

class SocketServer:
    """Socket server class to receive data from the sensor."""

    SO_TIMESTAMPNS = 35

    def __init__(self):
        self.running = False
        self.reading = False
        self.data_header_size = int(Sensor.get('headerlen', 36))
        self.data_report_size = int(Sensor.get('basedatalen', 52))
        self.headerOnlyMode: bool = int(os.getenv('SENSOR_HEADER_ONLY', 0)) == 1
        self.accepting_thread: Thread = None
        self.reading_thread: Thread = None
        self.socket: socket = None
        self.on_data_received: callable = None
        self.on_error: callable = None
        self.client: socket = None  # Track the client socket for closing later

        # NEW: trackers per timeout/diagnostica
        self.last_rx_ms = None                # timestamp ultimo pacchetto ricevuto (ms)
        self._accept_last_notify = 0.0        # per rate-limit delle notifiche di accept-timeout
        self.accept_watchdog_enabled = False  # attivo solo dopo start(2)

    def enable_accept_watchdog(self, enabled: bool):
        self.accept_watchdog_enabled = bool(enabled)

    def open(self):
        self.running = True
        try:
            host = os.getenv('SENSOR_HOST', 'localhost')
            port = int(os.getenv('SENSOR_PORT', 5000))
            log(f"[SocketServer] Opening the socket server at {host}:{port}")
            self.socket = socket(AF_INET, SOCK_STREAM)
            self.socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)

            try:
                self.socket.setsockopt(SOL_SOCKET, self.SO_TIMESTAMPNS, 1)
            except Exception:
                log("[SocketServer] SO_TIMESTAMPNS not supported, falling back to local timestamp", "debug")

            self.socket.bind((host, port))
            self.socket.listen(1)
            self.accepting_thread = Thread(target=self.__accepting, name="ServerSocketThread", daemon=True)
            self.accepting_thread.start()
            log(f"[SocketServer] Socket server opened at {host}:{port}")
        except Exception as e:
            self.close()
            msg = f"Error while opening the socket server: {e}"
            if self.on_error:
                self.on_error(msg)
            raise ValueError(msg)
        
    def close_connection(self):
        try:
            if self.client:
                log("[SocketServer] Closing the client connection...")
                self.client.close()
                self.client = None
        except Exception:
            pass

        if not self.reading:
            return
        
        self.reading = False

        # join del reader
        try:
            if self.reading_thread and self.reading_thread != current_thread():
                self.reading_thread.join(timeout=2.0)
                self.reading_thread = None
        except Exception as e:
            log(f"Error while closing the connection: {e}", "ERROR")

        log("[SocketServer] Client connection closed")
        
    def close(self):
        if not self.running:
            return
        
        log("[SocketServer] Closing the socket server...")
        self.running = False

        # Keep this order:
        # 1. Close the client connection if it exists
        try:
            if self.client:
                self.client.close()
                self.client = None
        except Exception:
            pass

        # 2. Join the reader thread, close immediately the listener to unlock the accept
        try:
            if self.socket:
                self.socket.close()
                self.socket = None
        except Exception:
            pass
        
        # 3. Join of the reader thread
        try:
            if self.reading_thread and self.reading_thread != current_thread():
                self.reading_thread.join(timeout=2.0)
                self.reading_thread = None
        except Exception as e:
            log(f"Error while closing the reading thread: {e}", "ERROR")

        # 4. Join the accepter thread
        try:
            if self.accepting_thread and self.accepting_thread != current_thread():
                self.accepting_thread.join(timeout=2.0)
                self.accepting_thread = None
        except Exception as e:
            log(f"Error while closing accepting thread: {e}", "ERROR")

        log("[SocketServer] Socket server closed")
        
    def __accepting(self):
        client: socket = None
        if (self.headerOnlyMode):
            size = self.data_header_size
        else:
            nreport = int(Sensor.get('nreports', 10))
            size = (self.data_header_size + nreport * self.data_report_size)

        # small timeout su accept per poter verificare l'accept-timeout e lo shutdown
        try:
            self.socket.settimeout(1.0)
        except Exception:
            pass

        # finestra di accept-timeout
        # default derivato da WDT se non presente
        _wdt = float(os.getenv("SENSOR_BOARD_WDT", "15"))
        accept_timeout = float(os.getenv("SENSOR_ACCEPT_TIMEOUT", str(_wdt + 5)))
        started = time.time()

        log(f"[SocketServer] Waiting for the connection...")
        while self.running:
            try:
                client, _ = self.socket.accept()
                self.client = client  # Store the client socket for later use

                log(f"[SocketServer] Connection from {client.getpeername()} accepted. Reading {size} bytes...")
                self.reading_thread = Thread(target=self.__reading, args=(client, size), name="ReadingThread", daemon=True)
                self.reading_thread.start()

                # reset timer (abbiamo accettato)
                started = time.time()

            # --- dentro SocketServer.__accepting (aggiungi debug prima della on_error) ---
            except timeout:
                if self.accept_watchdog_enabled and accept_timeout > 0 and (time.time() - started) >= accept_timeout:
                    reader_alive = bool(self.reading_thread and self.reading_thread.is_alive())
                    has_client   = self.client is not None
                    # DEBUG utile a capire scatti inattesi
                    #log(f"[SocketServer] accept-watchdog check: has_client={has_client}, reading={self.reading}, reader_alive={reader_alive}", "DEBUG")
                    if (not has_client) or (not self.reading) or (not reader_alive):
                        if self.on_error and self.running:
                            now = time.time()
                            if (now - self._accept_last_notify) >= accept_timeout:
                                self._accept_last_notify = now
                                self.on_error("Accept timeout: no client connected")
                continue

            except OSError as e:
                # Closed socket during shutdown or other socket error -> exit silently
                if not self.running or getattr(e, "winerror", None) in (10038,):
                    break
                if self.on_error:
                    self.on_error(f"[SocketServer] OSError during accept: {e}")
                break

        # End of thread
        log("[SocketServer] Accepting thread finished")
        self.accepting_thread = None
        if client:
            try:
                client.close()
            except Exception:
                pass

    # dentro class SocketServer
    def __recv_exact(self, client, size: int) -> bytes:
        buf = bytearray()
        while len(buf) < size:
            chunk = client.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("Client closed while reading")
            buf += chunk
        return bytes(buf)


    def __reading(self, client: socket, size: int):
        # Timeout configurabile (default 10s) per trigger recovery
        _wdt = float(os.getenv("SENSOR_BOARD_WDT_S", "15"))
        rx_default = max(6, int(_wdt // 2))
        rx_timeout = int(os.getenv('SENSOR_READ_TIMEOUT', rx_default))
        CHUNK = int(os.getenv("SENSOR_RX_CHUNK", "4096"))
        had_error = False

        has_recvmsg = hasattr(client, "recvmsg")
        ts_supported = True
        try:
            client.setsockopt(SOL_SOCKET, self.SO_TIMESTAMPNS, 1)
        except Exception:
            ts_supported = False

        try:
            client.settimeout(rx_timeout)
        except Exception:
            pass
        log(f"[SocketServer] recv timeout={rx_timeout}s", "INFO")
        self.reading = True
        while self.running and self.reading:
            try:
                import time as _t
                timestamp_ms = int(_t.time() * 1000)
                data = None

                if has_recvmsg and ts_supported:
                    # path Linux/*BSD: prova a usare kernel timestamp (SO_TIMESTAMPNS)
                    data, ancdata, flags, address = client.recvmsg(CHUNK, 1024)
                    if not data:
                        # Chiusura pulita lato peer
                        log("[SocketServer] Connection closed by the client")
                        self.reading = False
                        break

                    # prova a leggere il timestamp dal cmsg, se presente
                    for i in ancdata:
                        try:
                            if i[0] == SOL_SOCKET and i[1] == self.SO_TIMESTAMPNS:
                                sec, nsec = struct.unpack("ii", i[2][:8])
                                timestamp_ms = int(sec * 1000 + nsec * 1e-6)
                        except Exception:
                            pass
                else:
                    # path Windows: niente recvmsg → timestamp locale + lettura esatta
                    data = client.recv(CHUNK)
                    if not data:
                        # Chiusura pulita lato peer
                        log("[SocketServer] Connection closed by the client")
                        self.reading = False
                        break

                # aggiorna traccia ultima RX e inoltra al PacketHandler
                self.last_rx_ms = timestamp_ms
                if self.on_data_received:
                    self.on_data_received(data, timestamp_ms)

            except timeout:
                # Timeout reached, notify and close the connection (server remains up)
                if self.on_error and self.reading and self.running:
                    self.on_error("Timeout while reading data")
                had_error = True
                self.reading = False
                try:
                    client.close()
                except Exception:
                    pass
                break

            except Exception as e:
                # Real reading error: notify and close the connection only (server remains up)                
                if self.on_error and self.reading and self.running:
                    self.on_error(f"Error while reading data: {e}")
                had_error = True
                self.reading = False
                try:
                    client.close()
                except Exception:
                    pass
                break
                
        log("[SocketServer] Reading thread finished")
        # Notifica la chiusura SOLO se non c'è stato un errore già notificato
        if (not had_error) and self.on_error and self.running and not self.reading:
            self.on_error("Connection closed by the client")        # Notify the closing of the connection (if not already in error or in shutdown)

    # Helper opzionale per diagnostica
    def get_last_rx_age_s(self):
        if self.last_rx_ms is None:
            return None
        return max(0.0, time.time() - (self.last_rx_ms / 1000.0))
