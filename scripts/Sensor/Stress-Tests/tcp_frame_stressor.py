#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCP Frame Stressor per il modulo Sensor:
genera frame conformi all'handler (header 36B/40B, report 52B) e applica
variazioni (flapping, jitter, header-only, nreports variabile, framespezzati,
garbage, oversize/undersize, trigger allarmi).

Uso rapido:
  python tcp_frame_stressor.py --host 127.0.0.1 --port 5000 --scenario S2 --duration 10
  python tcp_frame_stressor.py --host 127.0.0.1 --port 5000 --scenario P1 --split 5
"""

import argparse, os, socket, time, random, struct
import sys, math, binascii, traceback
from typing import List, Tuple, Optional

try:
    from crc32_ieee import crc32_compute  # tabella/algoritmo come su board
except Exception:
    import zlib
    def crc32_compute(data: bytes) -> int:
        # fallback: CRC-32/IEEE (polinomio 0xEDB88320) compatibile con zlib
        return zlib.crc32(data) & 0xFFFFFFFF

# ---- utilities base: crea un frame "valido" con nreports ----
# Assunzioni: preambolo A55A (2B), header=40B (36 core + 4 CRC), report=52B.

HEADER_BASE_LEN = 36   # 36B fissi; con CRC diventano 40
HEADER_LEN = 40
REPORT_LEN = 52  # 8 ts + 8*4 floats + 3*4 floats

def now_us() -> int: 
    return time.time_ns() // 1_000

def connect(host, port, timeout=10.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    deadline = time.time() + timeout
    while True:
        try:
            s.connect((host, port))
            s.settimeout(None)
            return s
        except OSError as e:
            if time.time() >= deadline:
                raise
            time.sleep(0.2)


def send_all(s: socket.socket, data: bytes, *, split: int = 1, garbage: int = 0):
    """
    Invia 'data' con invii parziali (split>1) e, opzionalmente, aggiunge 'garbage' bytes dopo.
    - split=1   => invio unico (comportamento attuale)
    - split>1   => spezza 'data' in 'split' chunk ~equidimensione
    - garbage>0 => dopo 'data' invia os.urandom(garbage)
    """
    view = memoryview(data)
    if split <= 1:
        while view:
            n = s.send(view)
            view = view[n:]
    else:
        total = len(view)
        # chunk quasi equi; l’ultimo prende l’eventuale resto
        base = total // split
        off = 0
        for i in range(split):
            size = base if i < split - 1 else (total - off)
            if size <= 0:
                break
            chunk = view[off:off+size]
            while chunk:
                n = s.send(chunk)
                chunk = chunk[n:]
            off += size

    if garbage and garbage > 0:
        import os
        junk = os.urandom(int(garbage))
        vj = memoryview(junk)
        while vj:
            n = s.send(vj)
            vj = vj[n:]


def build_header(nreports: int,
                 header_only: bool = False,
                 stalta: int = 0,
                 fft: int = 0,
                 chmax: int = 0,
                 nChannels: int = 1,
                 tstamp_us: int = None,
                 metrics: Optional[List[float]] = None,
                 datafmt: int = 0,
                 header_crc: bool = True) -> bytes:
    """
    Header base = 36B:
      0..1  : sync A5 5A
      2     : pre1 => [fft(1) | stalta(1) | datafmt(2, 0) | nreports(4)]
      3     : pre2 => [chmax(2) | nch(1) | headerOnly(1) | reserved(4)]
      4..11 : tstamp_fft_us (uint64 LE)  # UNIX µs
      12..35: 6 * float32 metrics

    Se header_crc=True, appende 4 byte (uint32 LE) con CRC-32/IEEE calcolato sui primi 36B.
    """
    # Safety check
    assert nreports <= 10

    pre1 = ((1 if fft else 0) << 7) | ((1 if stalta else 0) << 6) | ((datafmt & 0x3) << 4) | (nreports & 0xF)
    pre2 = ((chmax & 0x3)) | ((1 if nChannels else 0) << 2) | ((1 if header_only else 0) << 3)
    sync = b'\xA5\x5A'

    if tstamp_us is None:
        tstamp_us = now_us()
    ts8 = struct.pack('<Q', int(tstamp_us))

    if metrics is None:
        metrics = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    mbytes = b''.join(struct.pack('<f', float(x)) for x in metrics[:6])

    head36 = sync + bytes([pre1, pre2]) + ts8 + mbytes
    assert len(head36) == HEADER_BASE_LEN

    if header_crc:
        # CRC-32 IEEE 802.3 (reflected) – stesso risultato di zlib.crc32
        crc = crc32_compute(head36)
        head = head36 + struct.pack('<I', crc)
        assert len(head) == HEADER_LEN
        return head
    else:
        return head36


def build_report(delta_ts:int, vals8:List[float], filt3:List[float]) -> bytes:
    """Timestamp nel payload (uint64 LE). Qui passiamo SEMPRE epoch µs assoluti."""
    if len(vals8) != 8: raise ValueError("vals8 deve essere 8 float")
    if len(filt3) != 3: raise ValueError("filt3 deve essere 3 float")
    ts = struct.pack('<Q', int(delta_ts))
    body = b''.join(struct.pack('<f', float(v)) for v in vals8)
    body += b''.join(struct.pack('<f', float(v)) for v in filt3)
    assert len(ts) + len(body) == REPORT_LEN
    return ts + body

def build_report_epoch(ts_epoch_us: int, accel: List[float] = None, filt: List[float] = None) -> bytes:
    vals8 = accel if accel is not None else [0.0]*8
    filt3 = filt  if filt  is not None else [0.0]*3
    return build_report(ts_epoch_us, vals8, filt3)

def build_report_delta(delta_us: int, accel: List[float] = None, filt: List[float] = None) -> bytes:
    """
    Wrapper: identico a build_report(), il primo campo è un delta in microsecondi dal primo campione.
    """
    vals8 = accel if accel is not None else [0.0]*8
    filt3 = filt  if filt  is not None else [0.0]*3
    return build_report(delta_us, vals8, filt3)


def one_frame(nreports:int, start_ts_us:int, ts_step_us:float) -> Tuple[bytes,int]:
    """Costruisce un frame completo (header+payload) con payload timestamps = epoch µs assoluti.
       Ritorna (frame_bytes, last_delta_us)."""
    headerOnly = False if nreports>0 else True
    header = build_header(nreports=nreports, header_only=headerOnly, stalta=0, fft=0, chmax=0, nChannels=1, tstamp_us=start_ts_us)
    payload = b''
    last_delta_us = 0
    for i in range(max(0, int(nreports))):
        ts_us = start_ts_us + int(round(i * ts_step_us))
        last_delta_us = ts_us - start_ts_us
        # valori fittizi (8 raw + 3 filt)
        vals8 = [0.1*i + 0.01*random.random() for i in range(8)]
        filt3 = [0.05*i for i in range(3)]
        payload += build_report_epoch(ts_us, vals8, filt3)
    return header + payload, last_delta_us




# ---------------- Scenari ----------------

def scenario_S2_flapping(host, port, duration=10.0, period=0.2):
    """Connect/close rapidi."""
    end = time.time() + duration
    while time.time() < end:
        try:
            s = connect(host, port, 2.0)
            # invia poco o nulla
            time.sleep(0.05)
            s.close()
        except Exception:
            time.sleep(period)

def scenario_S3_jitter(host, port, duration=10.0, nreports=10, freq=200, jitter_ms=(0,200)):
    s = connect(host, port)
    start = time.time()
    base_ts = now_us()
    while time.time() - start < duration:
        frame, _ = one_frame(nreports, base_ts, ts_step_us=1_000_000.0/freq)
        send_all(s, frame)
        time.sleep(random.uniform(jitter_ms[0], jitter_ms[1]) / 1000.0)
        base_ts += int(1_000_000.0*nreports/freq)
    s.close()

def scenario_S4_pause(host, port, warmup=0.0, pause=10.0, tail=0.0, freq=200):
    """Connetti → (opz) manda un frame di warmup → PAUSA lunga → (opz) manda un frame finale → chiudi."""
    import sys, time
    s = connect(host, port)
    base_ts = now_us()
    period_us = 1_000_000.0 / float(freq)

    print(f"[stressor] S4 start: warmup={warmup}s pause={pause}s tail={tail}s", file=sys.stdout, flush=True)

    # warmup: manda 1 frame (nr scelto in base al tempo, ma troncato a 10 per stare sul sicuro)
    if warmup > 0.0:
        nr = max(1, int(round(warmup * freq)))
        frame, _ = one_frame(nr, base_ts, period_us)
        print(f"[stressor] S4 warmup: send nreports={nr} bytes={len(frame)}", file=sys.stdout, flush=True)
        send_all(s, frame)
        base_ts += int(round(1_000_000.0 * nr / float(freq)))

    # PAUSA vera (socket vivo, nessun byte)
    time.sleep(max(0.0, pause))

    # tail: manda 1 frame finale
    if tail > 0.0:
        nr = max(1, int(round(tail * freq)))
        frame, _ = one_frame(nr, base_ts, period_us)
        print(f"[stressor] S4 tail:   send nreports={nr} bytes={len(frame)}", file=sys.stdout, flush=True)
        send_all(s, frame)

    s.close()
    print("[stressor] S4 done.", file=sys.stdout, flush=True)


def scenario_S5_throughput(host, port, duration=5.0, nreports=10, freq=1000):
    """
    Throughput “alto ma realistico”: timestamp ancorati al tempo reale e pacing = nreports/freq.
    Evita future-drift > slack lato Sensor.
    """
    s = connect(host, port)
    period_s_per_report = float(nreports) / float(freq)  # es. 10/200 = 0.05 s
    t_end = time.time() + float(duration)
    target_next = time.perf_counter()
    while time.time() < t_end:
        # Timestamp del frame = ora (t0), payload = t0 + k*step
        t0_us = now_us()
        frame, _ = one_frame(nreports, t0_us, 1_000_000.0/float(freq))
        send_all(s, frame)
        # pacing
        target_next += period_s_per_report
        sleep_left = target_next - time.perf_counter()
        if sleep_left > 0:
            time.sleep(sleep_left)
        else:
            # siamo in ritardo: riallinea target, così non accumuliamo drift
            target_next = time.perf_counter()
    s.close()


def scenario_P1_split(host, port, nframes=50, nreports=10, freq=200, split=5):
    s = connect(host, port)
    base_ts = now_us()
    for _ in range(nframes):
        frame, _ = one_frame(nreports, base_ts, 1_000_000.0/freq)
        send_all(s, frame, split=split)
        base_ts += int(1_000_000.0*nreports/freq)
    s.close()

def scenario_P2_garbage(host, port, nframes=20, nreports=10, freq=200, garbage=64):
    s = connect(host, port)
    base_ts = now_us()
    for _ in range(nframes):
        frame, _ = one_frame(nreports, base_ts, 1_000_000.0/freq)
        send_all(s, frame, garbage=garbage)
        base_ts += int(1_000_000.0*nreports/freq)
    s.close()

def scenario_P3_header_only(host, port, nheaders=20, freq=200):
    """
    Invia solo header validi (nreports=0) per nheaders volte.
    """
    print(f"[stressor] P3 start: nheaders={nheaders}", file=sys.stdout, flush=True)
    try:
        s = connect(host, port); base_ts = now_us(); period_us = 1_000_000.0/float(freq)
        for _ in range(nheaders):
            # header-only: nreports=0 e payload vuoto
            frame, _ = one_frame(0, base_ts, period_us)   # one_frame deve accettare nreports=0
            send_all(s, frame)
            # no-op su base_ts (nreports=0)
            time.sleep(0.002)
        s.close()
        print("[stressor] P3 done.", file=sys.stdout, flush=True)
        sys.exit(0)
    except Exception as e:
        traceback.print_exc(file=sys.stderr); sys.exit(1)


def scenario_P4_varnrep(host, port, duration=12.0, freq=200, seq=None):
    print(f"[stressor] P4 start: duration={duration}s", flush=True)
    try:
        s = connect(host, port); base_ts = now_us(); period_us = 1_000_000.0/float(freq)
        # sequenza di nreports variabile (parametrizzabile da CLI)
        seq = seq or [10, 8, 6, 4]
        t_end = time.time() + duration
        idx = 0
        while time.time() < t_end:
            nr = seq[idx % len(seq)]
            # header con nreports variabile (no header-only)
            head = build_header(nreports=nr, header_only=False, stalta=0, fft=0, chmax=20, nChannels=1)
            # payload coerente con nr: timestamp epoch µs
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b''.join(
                build_report_epoch(start_ts + int(round(i*step_us)), [0.01]*8, [0.0]*3) for i in range(nr)
            )
            frame = head + payload
            print(f"[stressor] P4 frame#{idx} nreports={nr} bytes={len(frame)}", flush=True)
            send_all(s, frame)  # nessun split/garbage qui
            idx += 1
            # cadenzamento: ~ nr/freq secondi per non saturare
            time.sleep(nr / float(freq))
        s.close()
        print("[stressor] P4 done.", flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def scenario_P5_oversize_undersize(host, port, duration=8.0, freq=200):
    """
    P5: Oversize/Undersize payload
    - header dichiara nreports=10
    - undersize: invio 9 report → mismatch got<exp (rilevato al frame successivo)
    - oversize: invio 11 report + (junk senza sync) + header frame successivo → mismatch got>exp (subito)
    """
    import os, time, sys, traceback, random

    def junk_no_sync(n: int) -> bytes:
        """bytes random *senza* sequenze A5 5A / 5A A5 (evita false sync nel junk)"""
        out = bytearray()
        last = None
        while len(out) < n:
            b = os.urandom(1)[0]
            if (last == 0xA5 and b == 0x5A) or (last == 0x5A and b == 0xA5):
                continue
            out.append(b)
            last = b
        return bytes(out)

    print(f"[stressor] P5 start: duration={duration}s", flush=True)
    p5_under = 0
    p5_over  = 0

    try:
        s = connect(host, port)
        t_end = time.time() + duration

        # alterna under/over per garantire entrambi i casi nella stessa run
        toggle = False

        while time.time() < t_end:
            nr_hdr = 10
            head = build_header(
                nreports=nr_hdr, header_only=False,
                stalta=0, fft=0, chmax=15, nChannels=1
            )

            # alterna: oversize (11 report) / undersize (9 report)
            do_over = toggle
            nr_payload = 11 if do_over else 9

            if do_over:
                p5_over += 1
            else:
                p5_under += 1

            # payload “nr_payload” report con epoch µs coerenti
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b"".join(build_report_epoch(start_ts + int(round(i*step_us)), [0.02]*8, [0.0]*3)
                        for i in range(nr_payload)
                    )

            if do_over:
                # OVERSIZE: aggiungi junk che NON contenga sync e poi *subito* un header del frame successivo
                # così il Sensor vede boundary≠sync, trova la prossima sync (j1) e logga got>exp.
                junk = junk_no_sync(8)  # pochi byte bastano, l’importante è “no sync” nel junk
                next_head = build_header(
                    nreports=nr_hdr, header_only=True,  # header-only valido dopo il resync
                    stalta=0, fft=0, chmax=15, nChannels=1
                )
                frame = head + payload + junk + next_head
            else:
                # UNDERSIZE: invia solo frame corto; il mismatch verrà rilevato quando arriva il frame successivo
                frame = head + payload

            print(f"[stressor] P5 sent header_nreports={nr_hdr} payload_reports={nr_payload} ({'OVER' if do_over else 'UNDER'})", flush=True)
            send_all(s, frame)

            toggle = not toggle
            time.sleep(0.05)  # coalescing TCP sufficiente a far vedere i confini

        s.close()
        print(f"[P5] summary: undersize={p5_under} oversize={p5_over}", flush=True)
        print("[stressor] P5 done.", flush=True)
        sys.exit(0)

    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)



def scenario_P6(host, port, headerlen=HEADER_LEN, basedatalen=REPORT_LEN,
                nreports=10, preamble="A55A", freq=200):
    """
    Inserisce la sync word (A55A/5AA5) DENTRO il payload per verificare che il parser non resync-chi.
    """
    print(f"[stressor] P6 start: nreports={nreports} preamble={preamble}", file=sys.stdout, flush=True)
    try:
        s = connect(host, port)
        base_ts = now_us()
        period_us = 1_000_000.0/float(freq)
        frame, _ = one_frame(nreports, base_ts, period_us)
        # inserisci sync dopo l'header (preservando TUTTI i 40B, CRC incluso)
        sync = binascii.unhexlify(preamble)
        payload = bytearray(frame[headerlen:])
        pos = min(len(payload)//2, len(payload)-len(sync)-1)  # posizione sicura
        payload[pos:pos+len(sync)] = sync
        frame = frame[:headerlen] + bytes(payload)
        send_all(s, frame)
        s.close()
        print("[stressor] P6 done.", file=sys.stdout, flush=True)
        sys.exit(0)
    except Exception as e:
        traceback.print_exc(file=sys.stderr); sys.exit(1)


def scenario_T1(host, port, nframes=20, nreports=10, freq=200, header_crc=False):
    """
    T1: invia nframes con nreports ciascuno.
    - Il campo timestamp nel payload è un UNIX epoch in microsecondi
      che avanza di step_us=1e6/freq a campione.
    """
    step_us = int(round(1_000_000 / max(1, int(freq))))
    s = connect(host, port)
    start_epoch_us = now_us()
    print(f"[stressor] T1: start_epoch_us={start_epoch_us} step_us={step_us} nframes={nframes}", flush=True)

    sent = 0
    last_ts_us = None

    try:
        for fidx in range(int(nframes)):
            # header: nreports costante, header_only=False
            head = build_header(nreports=nreports, header_only=False, stalta=0, fft=0, chmax=15, nChannels=1, header_crc=header_crc, tstamp_us=start_epoch_us + (fidx*nreports*step_us))

            # costruisci payload
            payload_chunks = []
            for r in range(int(nreports)):
                ts_us = start_epoch_us + (sent * step_us)
                last_ts_us = ts_us
                rpt = build_report_epoch(ts_us, accel=[0.02]*8, filt=[0.0]*3)
                payload_chunks.append(rpt)
                sent += 1

            frame = head + b"".join(payload_chunks)
            send_all(s, frame)

            # opzionale: un piccolo yield per non saturare tutto
            #time.sleep(0.001)
            time.sleep(0.1)

        s.close()
        print(f"[stressor] T1 done. first_ts_us={start_epoch_us} last_ts_us={last_ts_us} total_smples={sent} step_us={step_us}", flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        try:
            s.close()
        except Exception:
            pass
        sys.exit(1)



def scenario_A1_sta_lta(host, port, duration=35.0, freq=200, header_only_ratio=0.0):
    """
    A1: STA/LTA prolonged without FFT.
    Mantiene STALTA=1, FFT=0 per tutta la durata, per generare un RAISE all'inizio
    e nessun CLEAR finché dura lo scenario. Opzionale qualche header-only se vuoi.
    """
    print(f"[stressor] A1 start: duration={duration}s", flush=True)
    s = connect(host, port)
    period = 1.0 / float(freq)
    t_end = time.time() + float(duration)
    toggle_hdr = 0
    try:
        while time.time() < t_end:
            do_header_only = (header_only_ratio > 0.0 and (toggle_hdr % int(1.0 / max(1e-9, header_only_ratio)) == 0))
            head = build_header(nreports=10, header_only=do_header_only, stalta=1, fft=0, chmax=15, nChannels=1)

            if do_header_only:
                frame = head  # solo header
            else:
                start_ts = now_us()
                step_us  = 1_000_000.0/float(freq)
                payload = b"".join(build_report_epoch(start_ts + int(round(i*step_us)), [0.01]*8, [0.0]*3) for i in range(10))
                frame = head + payload

            send_all(s, frame)
            toggle_hdr += 1
            time.sleep(period)
        s.close()
        print("[stressor] A1 done.", flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def scenario_A2_fft_single(host, port, pre=3.0, post=3.0, freq=200):
    """
    A2: single FFT trigger.
    pre secondi con FFT=0, poi un breve burst (es. 3-5 frame) con FFT=1, poi post con FFT=0.
    Atteso: un RAISE e un CLEAR lato sensor.
    """
    print(f"[stressor] A2 start: pre={pre}s, post={post}s", flush=True)
    s = connect(host, port)
    period = 1.0 / float(freq)

    def send_frames(stalta, fft, seconds, nrep=10):
        t_end = time.time() + seconds
        while time.time() < t_end:
            head = build_header(nreports=nrep, header_only=False, stalta=stalta, fft=fft, chmax=10, nChannels=1)
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b"".join(build_report_epoch(start_ts + int(round(i*step_us)), [0.03]*8, [0.0]*3) for i in range(10))
            send_all(s, head + payload)
            time.sleep(period)

    try:
        # pre: no FFT
        send_frames(stalta=0, fft=0, seconds=float(pre))

        # breve burst con FFT=1 per innescare RAISE
        for _ in range(4):
            head = build_header(nreports=10, header_only=False, stalta=0, fft=1, chmax=12, nChannels=1)
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b"".join(build_report_epoch(start_ts + int(round(i*step_us)), [0.03]*8, [0.0]*3) for i in range(10))
            send_all(s, head + payload)
            time.sleep(period)

        # post: back to FFT=0 per innescare CLEAR
        send_frames(stalta=0, fft=0, seconds=float(post))

        s.close()
        print("[stressor] A2 done.", flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def scenario_A3_alarm_trigger(host, port, pre=3.0, post=3.0, freq=200):
    """
    Invio baseline per 'pre' secondi (stalta=1, fft=0), poi UN frame con fft=1,
    poi baseline per 'post' secondi
    per forzare i log:
      - "FFT flag set"
      - "Triggering alarm on channel"
      - "Sending triggered buffer to the alarm handler"
    """
    print(f"[stressor] A3 start: pre={pre}s post={post}s", flush=True)
    try:
        s = connect(host, port)
        # baseline pre
        t_end = time.time() + pre
        while time.time() < t_end:
            head = build_header(nreports=10, header_only=False, stalta=1, fft=0, chmax=10, nChannels=1)
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b''.join(build_report_epoch(start_ts + int(round(i*step_us)), [0.01]*8, [0.0]*3) for i in range(10))
            send_all(s, head + payload)
            time.sleep(10 / float(freq))
        # singolo trigger FFT
        head = build_header(nreports=10, header_only=False, stalta=1, fft=1, chmax=90, nChannels=1)
        start_ts = now_us()
        step_us  = 1_000_000.0/float(freq)
        payload = b''.join(build_report_epoch(start_ts + int(round(i*step_us)), [1.0]*8, [0.0]*3) for i in range(10))
        send_all(s, head + payload)
        # baseline post
        t_end = time.time() + post
        while time.time() < t_end:
            head = build_header(nreports=10, header_only=False, stalta=0, fft=0, chmax=10, nChannels=1)
            start_ts = now_us()
            step_us  = 1_000_000.0/float(freq)
            payload = b''.join(build_report_epoch(start_ts + int(round(i*step_us)), [0.01]*8, [0.0]*3) for i in range(10))
            send_all(s, head + payload)
            time.sleep(10 / float(freq))
        s.close()
        print("[stressor] A2 done.", flush=True)
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


SCENARIOS = {
    "S2": scenario_S2_flapping,
    "S3": scenario_S3_jitter,
    "S4": scenario_S4_pause,
    "S5": scenario_S5_throughput,
    "P1": scenario_P1_split,
    "P2": scenario_P2_garbage,
    "P3": scenario_P3_header_only,
    "P4": scenario_P4_varnrep,
    "P5": scenario_P5_oversize_undersize,
    "P6": scenario_P6,
    "T1": scenario_T1,
    "A1": scenario_A1_sta_lta,
    "A2": scenario_A2_fft_single,
    "A3": scenario_A3_alarm_trigger,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port",      type=int, default=5000)
    ap.add_argument("--scenario",  choices=sorted(SCENARIOS.keys()), required=True)
    ap.add_argument("--duration",  type=float, default=10.0, help="Durata in secondi (dove ha senso)")
    ap.add_argument("--freq",      type=float, default=200.0, help="Frequenza campionamento (Hz)")
    ap.add_argument("--header-crc", action="store_true", help="Append CRC32 (IEEE 802.3, LE) to header (makes header 40 bytes)")

    # P1: frame spezzati
    ap.add_argument("--nframes", type=int, default=20, help="Numero di frame da inviare (P1/P2/T1)")
    ap.add_argument("--split",   type=int, default=1, help="Numero di segmenti per frame (P1)")

    # P2: garbage tra i frame
    ap.add_argument("--garbage", type=int, default=0, help="Byte casuali tra i frame (P2)")

    # P3: header-only intermittente
    ap.add_argument("--nheaders", type=int, default=0, help="Numero di frame header-only (P3)")

    # P4: nreports variabile (già implementato: scenario_P4_varnrep)
    ap.add_argument("--seq", type=str, default="4,10,4,8,6", help="Sequenza nreports per P4 (es. 4,10,4,8,6)")

    # P5: oversize/undersize
    ap.add_argument("--nreports", type=int, default=10, help="nreports per P5/P6")

    # P6: sync dentro il payload
    ap.add_argument("--headerlen",   type=int, default=36, help="Lunghezza header (P6)")
    ap.add_argument("--basedatalen", type=int, default=52, help="Lunghezza report (P6)")
    ap.add_argument("--preamble",    type=str, default="A55A", help="Sync word esadecimale (P6)")

    # A1: alarm STA/LTA with headerOnly ratio
    ap.add_argument("--headerOnlyRatio", type=float, default=0.0, help="Header only ratio (A1)")

    # A2: finestra pre/post
    ap.add_argument("--pre",  type=float, default=3.0, help="Secondi prima dell'evento (A2)")
    ap.add_argument("--post", type=float, default=3.0, help="Secondi dopo l'evento (A2)")

    # S4 (pausa lunga) - li avevamo già aggiunti, ma ricordo qui per completezza
    ap.add_argument("--warmup", type=float, default=0.0, help="S4: secondi di traffico prima della pausa")
    ap.add_argument("--pause",  type=float, default=10.0, help="S4: durata pausa senza traffico")
    ap.add_argument("--tail",   type=float, default=0.0, help="S4: secondi di traffico dopo la pausa")

    args = ap.parse_args()

    fn = SCENARIOS[args.scenario]
    kw = {}
    if args.scenario == "S2":
        kw = dict(duration=args.duration)
    elif args.scenario == "S3":
        kw = dict(duration=args.duration)
    elif args.scenario == "S4":
        kw = dict(warmup=args.warmup, pause=args.pause, tail=args.tail)
    elif args.scenario == "S5":
        kw = dict(duration=args.duration)
    elif args.scenario == "P1":
        # spezza ogni frame in N parti
        kw = dict(nframes=args.nframes, split=args.split, freq=args.freq)
    elif args.scenario == "P2":
        # inserisci 'garbage' bytes tra un frame e il successivo
        kw = dict(nframes=args.nframes, garbage=args.garbage, freq=args.freq)
    elif args.scenario == "P3":
        # invia solo header per nheaders, poi un po' di frame normali
        kw = dict(nheaders=args.nheaders, freq=args.freq)
    elif args.scenario == "P4":
        # già presente: usa la seq (stringa "4,10,...")
        seq = [int(x) for x in args.seq.split(",") if x.strip()]
        kw = dict(seq=seq, freq=args.freq)
    elif args.scenario == "P5":
        # undersize + oversize
        kw = dict(duration=args.duration, freq=args.freq)
    elif args.scenario == "P6":
        # sync dentro il payload (no early resync)
        kw = dict(headerlen=args.headerlen,
                  basedatalen=args.basedatalen,
                  nreports=args.nreports,
                  preamble=args.preamble,
                  freq=args.freq)
    elif args.scenario == "T1":
        # undersize + oversize
        kw = dict(nframes=args.nframes, nreports=10, freq=args.freq, header_crc=bool(args.header_crc))
    elif args.scenario == "A1":
        # STA-LTA prolungato no FFT con HeaderOnly opzionali
        kw = dict(duration=args.duration, freq=args.freq, header_only_ratio=args.headerOnlyRatio)
    elif args.scenario == "A2":
        # FFT prolungato no STA-LTA
        kw = dict(pre=args.pre, post=args.post, freq=args.freq)
    elif args.scenario == "A3":
        # evento singolo con finestra pre/post
        kw = dict(pre=args.pre, post=args.post, freq=args.freq)

    fn(args.host, args.port, **kw)
    return 0

if __name__ == "__main__":
    import sys, traceback
    try:
        sys.exit(main())
    except SystemExit as e:
        raise
    except Exception:
        traceback.print_exc()  # va su stderr e finisce in stressor_stderr.log
        sys.exit(1)
