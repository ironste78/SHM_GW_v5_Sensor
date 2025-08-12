from modules.tools.log import log
from socket import socket, timeout, AF_INET, SOCK_DGRAM, IPPROTO_UDP
import os, time

class UDPSender:

    @staticmethod
    def sendUnicast(ipDest: str):
        client_socket = None
        try:
            client_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
            #client_socket.setsockopt(SOL_SOCKET, SO_BROADCAST, 1)
            
            ipPort = int(os.getenv('DISCOVERY_BOARD_PORT', '1110'))
            client_socket.sendto(f"SHM_DISCOVERY_Req=1;tStamp={int(time.time()*1000)}\0".encode(), (ipDest, ipPort))
            
            start_time = time.time()
            window = 10.0  # seconds
            while True:
                elapsed_time = time.time() - start_time
                if elapsed_time > window:
                    break
                # Mantiene timeout coerente con il tempo residuo
                client_socket.settimeout(max(0.1, window - elapsed_time))
                try:
                    data, addr = client_socket.recvfrom(1024)

                    log(f"Received response from {addr}: {data}")
                except timeout:
                    continue

        except Exception as e:
            log(f"Error while sending broadcast: {e}", "ERROR")
        finally:
            try:
                if client_socket:
                    client_socket.close()
            except Exception as e:
                pass

