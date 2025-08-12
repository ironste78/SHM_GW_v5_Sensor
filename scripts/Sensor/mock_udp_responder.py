#!/usr/bin/env python3
"""
mock_udp_responder.py
Mock "board" that:
  1) Replies to UDP discovery with tuple format:  > (HOSTNAME,UUID,IP:TCPPORT,BOARD_STATUS)\r\n
  2) Exposes a TCP "console" compatible with your SocketClient ("1", "C host:port", "2", "3", "7 1", "Q")
  3) (optional) After receiving "2" (start), connects to the server given by "C host:port" and
     streams binary frames that match your C structs:
        report_t {
          headerReport_t header;           // 36 bytes
          adcData_t      dataPayload[N];   // N * 52 bytes
        }
     Cadence: N / sampling_freq seconds (e.g., 10 / 200 = 0.05s).
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import random
import struct
import math
from typing import Optional

# --- robust import del CRC tabellare ---
try:
    from crc32_ieee import crc32_compute  # CRC-32 (IEEE 802.3) tabellare, LE
except ImportError:
    # prova ad aggiungere la cartella "Stress-Tests" accanto a questo script
    _here = os.path.dirname(os.path.abspath(__file__))
    _alt  = os.path.join(_here, "Stress-Tests")
    if os.path.isdir(_alt) and _alt not in sys.path:
        sys.path.insert(0, _alt)
    from crc32_ieee import crc32_compute


PROMPT = b"SHM_console#"

# -----------------------------
# Utility
# -----------------------------
def gen_mac():
    return "02:" + ":".join(f"{random.randint(0,255):02x}" for _ in range(5))

def get_local_ip(fallback="127.0.0.1"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return fallback

# -----------------------------
# UDP Discovery responder (tuple format)
# -----------------------------
def udp_responder(bind_host, udp_port, hostname, uuid, adv_ip, tcp_port, board_status, verbose=False):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((bind_host, udp_port))
        if verbose:
            print(f"[mock-udp] listening on {bind_host}:{udp_port} (tuple reply: > ({hostname},{uuid},{adv_ip}:{tcp_port},{board_status}))")
        while True:
            try:
                data, addr = s.recvfrom(2048)
                msg = data.decode(errors="ignore")
                if "SHM_DISCOVERY_Req" in msg:
                    resp = f"> ({hostname},{uuid},{adv_ip}:{tcp_port},{board_status})\r\n"
                    s.sendto(resp.encode(), addr)
                    if verbose:
                        print(f"[mock-udp] <- {addr}: {msg.strip()}")
                        print(f"[mock-udp] -> {addr}: {resp.strip()}")
            except Exception as e:
                if verbose:
                    print(f"[mock-udp] recv error: {e}")
                continue
    except Exception as e:
        print(f"[mock-udp] fatal: {e}")
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass

# clock di riferimento stile board (tStart + contatore µs derivato da monotonic)
tStart_us: int = int(time.time() * 1_000_000)
mono0_ns: int  = time.monotonic_ns()

def now_us(jitter_us: int = 0) -> int:
    """UNIX time in µs calcolato come tStart + (monotonic - mono0) + jitter opzionale."""
    elapsed_us = (time.monotonic_ns() - mono0_ns) // 1_000
    ts = tStart_us + elapsed_us
    if jitter_us:
        ts += random.randint(-jitter_us, jitter_us)
    return ts

# -----------------------------
# Frame generator (binary payload)
# -----------------------------
def build_header(nreports: int, data_format: int, sta: int, fft: int, chmax: int, numchs_flag: int, header_only: int,
                 tstamp_fft_us: int, slta: float, maxperc: float, iqr: float, rms: float, peak: float, cav: float,
                 header_crc: bool=False) -> bytes:
    """
    headerReport_t {
      framePreamble_t preamble;  // synch (uint16=0xA55A), then 1 byte flags1, 1 byte flags2
      uint64  tStampFFT;
      float32 SLTAratio, maxPerc, IQR, RMS, PEAK, CAV;
    }
    All in little-endian.
    """
    synch = 0xA55A
    # flags1: [STAflag:1 | FFTflag:1 | dataFormat:2 | numData:4]  (bit order within a byte is implementation-defined;
    # we pack as: numData low 4 bits, then dataFormat(2), STA(1), FFT(1) in the high bits)
    flags1 = ((nreports & 0x0F)
              | ((data_format & 0x03) << 4)
              | ((sta & 0x01) << 6)
              | ((fft & 0x01) << 7))
    # flags2: [res2:4 | txHeaderOnly:1 | numChs:1 | chMaxPerc:2] (we'll use chMaxPerc in 0..3 as placeholder)
    flags2 = ((0 & 0x0F) << 4) | ((header_only & 0x01) << 3) | ((numchs_flag & 0x01) << 2) | (chmax & 0x03)

    # pack primi 36 byte
    hdr36 = struct.pack("<HBBQ6f", synch, flags1, flags2, int(tstamp_fft_us), slta, maxperc, iqr, rms, peak, cav)
    if not header_crc:
        return hdr36
    # Append CRC32 LE calcolato sui primi 36 byte
    crc = crc32_compute(hdr36) & 0xFFFFFFFF
    return hdr36 + struct.pack("<I", crc)

def build_adc(ts_us: int, avg8, accfir3) -> bytes:
    """adcData_t { uint64 tStamp; float32 AVG[8]; float32 accFIR[3]; } — tStamp in **µs UNIX**"""
    return struct.pack("<Q8f3f", int(ts_us), *avg8, *accfir3)

def build_report(nreports: int, nchs: int, base_ts_us: int, freq_hz: int, channels_map: str,
                 header_only: bool=False, header_crc: bool=False, jitter_us: int = 0, epoch_payload: bool = True) -> bytes:
    """
    Costruisce un frame completo (header + N*adc). Usa un'onda sin/cos per simulare i canali.
    """
    # Header
    sta = 0
    fft = 0
    data_format = 2  # mV (per il tuo commento)
    chmax = 0        # segnaposto
    numchs_flag = 0 if nchs == 8 else 1

    # timestamp FFT (momento "calcolo metriche") in µs UNIX
    tstamp_fft_us = now_us(jitter_us)
    
    # metriche finte
    slta = 1.0
    maxperc = 0.0
    iqr = 0.5
    rms = 0.1
    peak = 0.2
    cav = 0.0
    
    header = build_header(nreports, data_format, sta, fft, chmax, numchs_flag, 1 if header_only else 0,
                          int(tstamp_fft_us), slta, maxperc, iqr, rms, peak, cav, header_crc=header_crc)

    if header_only:
        return header

    # Data payload
    payload = []
    dt = 1.0 / float(max(1, freq_hz))
    step_us = int(round(dt * 1_000_000))
    for k in range(nreports):
        # timestamp campione in µs:
        #   - epoch_payload=True  → assoluto UNIX epoch (µs)
        #   - epoch_payload=False → delta (0, step, 2*step, …) in µs
        if epoch_payload:
            ts_us = base_ts_us + k * step_us
            if jitter_us:
                ts_us += random.randint(-jitter_us, jitter_us)
        else:
            ts_us = k * step_us

        # 8 canali
        avg8 = []
        for ch in range(8):
            phase = 2.0 * math.pi * (ch+1) * (k / max(1.0, nreports))
            val = 0.01 * math.sin(phase)  # piccola ampiezza in mV
            avg8.append(val)
        accfir3 = [0.0, 0.0, 0.0]
        payload.append(build_adc(ts_us, avg8, accfir3))
    return header + b"".join(payload)

# -----------------------------
# TCP Console and frame streaming
# -----------------------------
class ConsoleState:
    def __init__(self, uuid, mac, nch, freq, nreports, channels_map, header_crc: bool,
                 jitter_us: int = 0, epoch_payload: bool = False):
        self.uuid = uuid
        self.mac = mac
        self.nch = nch
        self.freq = freq
        self.nreports = nreports
        self.channels_map = channels_map  # 8-char string "11111111"
        self.header_crc = bool(header_crc)
        self.jitter_us = int(jitter_us)
        self.epoch_payload = bool(epoch_payload)
        self.server_target = None  # (host, port) after "C host:port"
        self.sampling = False
        self.sender_thread = None
        self.sender_stop = threading.Event()
        self.send_frames_enabled = False  # toggled by CLI

    def info_payload(self):
        return {
            "UUID": self.uuid,
            "MAC": self.mac,
            "procPid": str(os.getpid()),
            "frequency": self.freq,
            "nreports": self.nreports,
            "nchannels": self.nch,
            "channels": self.channels_map,
            "headerlen": 40 if self.header_crc else 36,            
            "basedatalen": 52,
            "gateway_port": 0
        }

def frame_sender_loop(state: ConsoleState, verbose=False):
    """Connects to the configured server and streams frames at cadence N/freq seconds while sampling=True."""
    while state.sampling and state.send_frames_enabled:
        if not state.server_target:
            time.sleep(0.1)
            continue
        host, port = state.server_target
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(5)
            sock.connect((host, port))
            if verbose:
                print(f"[mock-stream] connected to server {host}:{port}")
            # cadence
            interval = state.nreports / float(state.freq if state.freq > 0 else 200)
            while state.sampling and state.send_frames_enabled and not state.sender_stop.wait(timeout=interval):
                # base_ts in µs UNIX dal "contatore" + tStart
                base_ts = now_us(state.jitter_us)
                hdr_only = False
                
                frame = build_report(
                    state.nreports, state.nch, base_ts, state.freq, state.channels_map,
                    header_only=hdr_only, header_crc=state.header_crc,
                    jitter_us=state.jitter_us, epoch_payload=state.epoch_payload
                )
                sock.sendall(frame)
                if verbose:
                    print(f"[mock-stream] -> sent frame: {len(frame)} bytes")
        except Exception as e:
            if verbose:
                print(f"[mock-stream] error: {e} (will retry)")
            time.sleep(0.5)
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        # small backoff before reconnect
        time.sleep(0.2)


def handle_console_client(conn, addr, state: ConsoleState, verbose=False):
    conn.settimeout(10)
    try:
        conn.sendall(PROMPT)
        buf = b""
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buf += data
            msg = buf.decode(errors="ignore").strip()
            buf = b""
            if verbose:
                print(f"[mock-tcp] <- {addr}: {msg!r}")

            if msg.upper().startswith("Q"):
                break

            elif msg.startswith("1"):
                resp = json.dumps(state.info_payload())
                conn.sendall(resp.encode() + PROMPT)
                if verbose:
                    print(f"[mock-tcp] -> info: {resp}")
                return  # <<< chiudi dopo una risposta

            elif msg.startswith("C "):
                try:
                    _, hp = msg.split(" ", 1)
                    h, p = hp.split(":", 1)
                    state.server_target = (h.strip(), int(p.strip()))
                    if verbose:
                        print(f"[mock-tcp] configured server target: {state.server_target}")
                    conn.sendall(b"[OK]")
                except Exception as e:
                    if verbose:
                        print(f"[mock-tcp] bad C command: {e}")
                    conn.sendall(b"[KO]")
                return  # <<< chiudi

            elif msg.startswith("2"):
                state.sampling = True
                if state.send_frames_enabled and (state.sender_thread is None or not state.sender_thread.is_alive()):
                    state.sender_stop.clear()
                    state.sender_thread = threading.Thread(target=frame_sender_loop, args=(state, verbose), daemon=True)
                    state.sender_thread.start()
                conn.sendall(b"[OK]")
                if verbose:
                    print(f"[mock-tcp] -> [OK] for start (send_frames_enabled={state.send_frames_enabled})")
                return  # <<< chiudi

            elif msg.startswith("3"):
                state.sampling = False
                state.sender_stop.set()
                conn.sendall(b"[OK]")
                if verbose:
                    print(f"[mock-tcp] -> [OK] for stop")
                return  # <<< chiudi

            elif msg.startswith("7 1"):
                state.sampling = False
                state.sender_stop.set()
                # mantieni il server_target
                conn.sendall(b"[OK]")
                if verbose:
                    print(f"[mock-tcp] -> [OK] for reset")
                return  # <<< chiudi

            else:
                conn.sendall(PROMPT)

    except Exception as e:
        if verbose:
            print(f"[mock-tcp] error with {addr}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def tcp_console_server(bind_host, tcp_port, state: ConsoleState, verbose=False):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((bind_host, tcp_port))
        s.listen(5)
        print(f"[mock-tcp] console listening on {bind_host}:{tcp_port}")
        while True:
            conn, addr = s.accept()
            if verbose:
                print(f"[mock-tcp] connect from {addr}")
            t = threading.Thread(target=handle_console_client, args=(conn, addr, state, verbose), daemon=True)
            t.start()
    except Exception as e:
        print(f"[mock-tcp] fatal: {e}")
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass

# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uuid", required=True, help="UUID to advertise (must match the Sensor UUID)")
    ap.add_argument("--mac", default=gen_mac(), help="MAC address to advertise in console info")
    ap.add_argument("--ip", default=get_local_ip(), help="IP address advertised back to discovery for tuple reply")
    ap.add_argument("--udp-host", default="0.0.0.0", help="UDP bind host for discovery responses")
    ap.add_argument("--udp-port", type=int, default=int(os.getenv("DISCOVERY_BOARD_PORT", 1110)), help="UDP discovery port")
    ap.add_argument("--tcp-host", default="0.0.0.0", help="TCP bind host for console")
    ap.add_argument("--tcp-port", type=int, default=1105, help="TCP console port")
    ap.add_argument("--board-status", type=int, default=1, help="Status value in tuple reply (last field)")
    ap.add_argument("--nch", type=int, default=8, choices=[4,8], help="Number of channels reported by '1'")
    ap.add_argument("--freq", type=int, default=200, help="Sampling frequency (Hz) reported by '1'")
    ap.add_argument("--nreports", type=int, default=10, help="NUM_DATA_PER_REPORT reported by '1'")
    ap.add_argument("--channels-map", default="11111111", help="8-char map of channels expected by PacketHandler (digits 1..5)")
    ap.add_argument("--send-frames", action="store_true", help="If set, after '2' the mock connects to the server and streams frames")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    ap.add_argument("--header-crc", action="store_true", help="Append CRC32 (IEEE 802.3, LE) to header (makes header 40 bytes)")
    ap.add_argument("--jitter-us", type=int, default=0, help="± jitter in microseconds to add to each sample timestamp")
    ap.add_argument("--epoch-payload", action="store_true",
                    help="If set, payload timestamps are absolute UNIX epoch microseconds; otherwise deltas in microseconds starting at 0.")    
    args = ap.parse_args()

    print(f"[mock] epoch payload: {'ENABLED' if args.epoch_payload else 'disabled'}", flush=True)

    hostname = socket.gethostname()
    state = ConsoleState(args.uuid, args.mac, args.nch, args.freq, args.nreports, args.channels_map,
                         header_crc=args.header_crc, jitter_us=args.jitter_us, epoch_payload=args.epoch_payload)

    state.send_frames_enabled = bool(args.send_frames)

    # start UDP and TCP
    th_udp = threading.Thread(
        target=udp_responder,
        args=(args.udp_host, args.udp_port, hostname, args.uuid, args.ip, args.tcp_port, args.board_status, args.verbose),
        daemon=True
    )
    th_tcp = threading.Thread(
        target=tcp_console_server,
        args=(args.tcp_host, args.tcp_port, state, args.verbose),
        daemon=True
    )
    th_udp.start()
    th_tcp.start()

    print(f"[mock] ready. UUID={args.uuid} UDP={args.udp_host}:{args.udp_port} TCP={args.tcp_host}:{args.tcp_port} "
          f"tuple advertises ({hostname},{args.uuid},{args.ip}:{args.tcp_port},{args.board_status}); "
          f"send_frames={state.send_frames_enabled} header_crc={'on' if state.header_crc else 'off'} "
          f"jitter_us=±{state.jitter_us}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("[mock] stopping...")

if __name__ == "__main__":
    main()
