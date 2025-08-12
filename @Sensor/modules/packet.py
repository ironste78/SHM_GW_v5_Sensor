from dataclasses import dataclass
from threading import Thread, current_thread
from queue import Queue, Empty, Full
import os, time, traceback

from modules.sensor import Sensor
from modules.storer import AccelerometerStorer
from modules.tools.log import log

import zlib
from typing import ByteString


@dataclass
class HeaderInfo:
    # preamble / flags
    flag_stalta: int
    flag_fft: int
    data_format: int
    nreports: int
    chmaxPerc: int
    nChannelsFlag: int      # 0 => 8ch, 1 => 4ch
    headerOnlyMode: int     # 0/1 (mantengo int per compatibilità log/pattern)
    # campi numerici dall’header
    tStampFFT: int
    metrics: list           # [STA/LTA, MAX%, IQR, RMS, PEAK, CAV] come bytes (come prima)


class PacketHandler:
    """Packet handler con pipeline a 4 passaggi:
       1) validate format → estrae frame completo
       2) parse header content → HeaderInfo
       3) evaluate alarms
       4) parse payload content
    """

    # accetta entrambi i preamboli (A5 5A e 5A A5)
    _SYNC_SET = (b'\xA5\x5A', b'\x5A\xA5')

    # ---------- lifecycle ----------

    def __init__(self, on_alarm_received: callable = None):
        self.__init_variables()
        # Fallback no-op per compatibilità
        self.on_alarm_received = on_alarm_received or (lambda *a, **k: None)

    def start(self):
        """Start the packet handler"""
        self.__init_variables()
        self.running = True
        self.thread = Thread(target=self.__on_packet_parsing, name="PacketHandler", daemon=True)
        self.thread.start()
        log("[PacketHandler] Packet handler is running...", "INFO")

    def stop(self):
        """Stop the packet handler"""
        log("[PacketHandler] Stopping the packet handler...", "INFO")
        self.running = False
        try:
            if self.thread and self.thread != current_thread():
                self.thread.join()
        except Exception as e:
            log(f"[PacketHandler] Error while stopping the packet handler: {e}", "ERROR")
        self.__init_variables()
        log("[PacketHandler] Packet handler stopped", "INFO")

    def add_packet(self, data_buffer: bytes, tStamp: int = None):
        """Add a packet to the packet handler"""
        if not data_buffer:
            return
        try:
            self.queue.put_nowait([data_buffer, tStamp])
        except Full:
            self._drop_count += 1
            now = time.time()
            if (now - self._last_drop_log_ts) > 5.0:
                self._last_drop_log_ts = now
                log(f"[PacketHandler] queue full → dropped {self._drop_count} chunks so far", "WARNING")

        if (not self.thread) or (not self.thread.is_alive()):
            # auto-restart del worker
            self.running = True
            self.thread = Thread(target=self.__on_packet_parsing, name="PacketHandler", daemon=True)
            self.thread.start()

    # ---------- internals ----------

    def __init_variables(self):
        """Initialize the variables"""
        self.running: bool = False

        # stato allarmi / eventi
        self.buffer_history: list = []
        self.alarm_state: bool = False
        self.triggered: bool = False
        self.EVENT_PRE_MS  = int(os.getenv("EVENT_PRE_MS",  "30000"))  # -30 s
        self.EVENT_POST_MS = int(os.getenv("EVENT_POST_MS", "45000"))  # +45 s
        # Flags to log/track the state transitions of the STA/LTA and FFT flags
        self._stalta_active = False
        self._fft_active = False

        self.header_crc32_enabled = os.environ.get("SENSOR_ENABLE_HEADER_CRC32", "0") not in ("0", "false", "False", "")
        self.header_crc32_strict  = os.environ.get("SENSOR_HEADER_CRC_STRICT",  "1") not in ("0", "false", "False", "")
        # nota: se abiliti CRC ma headerlen < 40, lo skipperemo con un warning una sola volta
        self._crc_cfg_warned = False

        # triggered_buffer_list = [ [t_trigger, last_alarm_ts, buf_or_None, t_close] , ... ]
        self.triggered_buffer_list: list = []
        self.triggered_timestamp: int = None
        self.alarm_state_timestamp: int = None

        self._last_saved_ts = None

        # coda di input
        qmax = int(os.getenv("PACKET_QUEUE_MAX", "200"))
        self.queue: Queue = Queue(maxsize=qmax)
        self.thread: Thread = None
        self.storer: AccelerometerStorer = AccelerometerStorer()

        # buffer di framing
        self.buffer: bytes = b''
        self._max_buffer = int(os.getenv("PACKET_BUFFER_MAX", str(4 * 1024 * 1024)))  # 4MB default
        self._last_buffer_warn = 0.0

        self._drop_count = 0
        self._last_drop_log_ts = 0.0

        # configurazione di formattazione dati
        dataHeaderLen = 40 if self.header_crc32_enabled else 36  # 36B header + 4B CRC32
        self.data_header_size = int(Sensor.get('headerlen', dataHeaderLen))
        self.data_report_size = int(Sensor.get('basedatalen', 52))
        self.headerOnlyMode_default: bool = int(os.getenv('SENSOR_HEADER_ONLY', 0)) == 1  # solo fallback
        self.is_filtered: bool = int(os.getenv('SENSOR_DATA_FILTERED', 0)) == 1
        self.nreports_default: int = int(Sensor.get('nreports', 0))
        self.channels: str = Sensor.get('channels', None)
        if self.nreports_default == 0 or self.channels is None or len(self.channels) != 8:
            raise ValueError("[PacketHandler] Invalid configuration")

        self.frequency = int(Sensor.get('frequency', 200))

        # --- timestamp sanity config (unificato)
        # SENSOR_TS_UNIT: ms | us | auto  (auto: >1e14 => us, altrimenti ms)
        self.ts_unit = os.getenv("SENSOR_TS_UNIT", "auto").lower()

        # compat alias -> valore unico
        def _env(name, default):
            return os.getenv(name, default)

        # futuro: usa FUTURE_SLACK se presente, altrimenti FUTURE_MAX
        self.ts_future_slack_ms = int(_env("SENSOR_TS_FUTURE_SLACK_MS",
                                      _env("SENSOR_TS_FUTURE_MAX_MS", "2000")))
        # backstep: usa BACKSTEP_TOL se presente, altrimenti BACKWARD_MAX
        self.ts_backstep_tol_ms = int(_env("SENSOR_TS_BACKSTEP_TOL_MS",
                                      _env("SENSOR_TS_BACKWARD_MAX_MS", "0")))

        # payload checks
        self.ts_check_enabled     = _env("SENSOR_TS_CHECK_ENABLED", "1") not in ("0","false","no","False")
        self.ts_drop_on_violation = _env("SENSOR_TS_DROP_ON_VIOLATION", "0") not in ("0","false","no","False")

        # header policy (rimpiazza ts_enforce) — compat: se TS_ENFORCE=1, droppa su violazione header
        self.ts_header_drop_on_violation = _env("SENSOR_TS_HEADER_DROP_ON_VIOLATION",
                                           _env("SENSOR_TS_ENFORCE", "0")) not in ("0","false","no","False")

        self._last_header_ts_ms = None

        # --- frame-to-frame time anchoring (continuity) ---
        # ancoraggio tra frame basato sui delta del payload (ms)
        self._anchor_epoch_ms = None      # epoch ms del primo campione del frame corrente
        self._anchor_delta0_ms = None     # delta (ms) del primo campione del frame corrente
        # differenza massima accettata tra "predicted_first" e "rcv_ms" per ri-ancorare al clock locale
        self.ts_frame_anchor_tol_ms = int(os.getenv("SENSOR_TS_FRAME_ANCHOR_TOL_MS", "50"))

        # stato interno per controllo monotonia su timeline normalizzata (ms)
        self._last_adj_ts_ms = None

        # passo nominale tra campioni (ms), es. 200 Hz → 5 ms
        self.sample_step_ms = max(1, int(round(1000 / max(1, self.frequency))))

        # correzione “una tantum” dell’ancora, con un tetto di sicurezza
        self.ts_correct_on_backstep = int(os.getenv("SENSOR_TS_CORRECT_ON_BACKSTEP", "1")) == 1
        self.ts_correct_max_ms      = int(os.getenv("SENSOR_TS_CORRECT_MAX_MS", "1000"))   # clamp

        if self.ts_backstep_tol_ms == 0:  # già esiste nel tuo codice
            self.ts_backstep_tol_ms = max(1, self.sample_step_ms // 2)

        # throttle per messaggi ripetitivi (mismatch/confine)
        self._mm_last = {}          # key -> last monotonic ts
        self._mm_min_interval = 0.3 # secondi tra due log identici

        # (eventuale) allineamento tempi reale / µs (non usato ora)
        # self.align_epoch_ms: Optional[int] = None
        # self.align_first_us: Optional[int] = None

    def set_alignment_epoch(self, epoch_ms: int) -> None:
        self.align_epoch_ms = int(epoch_ms)
        self.align_first_us = None  # si resetta: il prossimo frame fisserà la base
        log(f"[PacketHandler] Alignment epoch set to {self.align_epoch_ms} ms", "DEBUG")

    # ---------- timestamp helpers ----------
    def _normalize_header_ts_ms(self, raw: int) -> int:
        """Converte il timestamp dell'header in millisecondi in base a SENSOR_TS_UNIT."""
        return int(raw) if self.ts_unit == "ms" else int(raw // 1000)

    def _guard_header_timestamp(self, ts_ms: int) -> bool:
        """Controlla che il timestamp di header non sia troppo nel futuro e non regredisca oltre la tolleranza."""
        now_ms = int(time.time() * 1000)
        ok = True

        # futuro eccessivo
        if ts_ms > now_ms + self.ts_future_slack_ms:
            delta = ts_ms - now_ms
            log(f"[PacketHandler] header timestamp too far in the future: +{delta} ms (ts={ts_ms}, now={now_ms})", "WARNING")
            ok = False

        # backstep oltre soglia
        if self._last_header_ts_ms is not None:
            back = self._last_header_ts_ms - ts_ms
            if back > self.ts_backstep_tol_ms:
                log(f"[PacketHandler] header timestamp regressed by {back} ms (now={ts_ms}, prev={self._last_header_ts_ms})", "WARNING")
                ok = False

        # mantieni monotonia “non decrescente”
        self._last_header_ts_ms = max(ts_ms, self._last_header_ts_ms or ts_ms)
        return ok

    def _ts_check(self, adj_ts_ms: int) -> bool:
        """
        Controlli sul timestamp payload (sempre epoch ms):
        - non troppo nel futuro rispetto all'epoch locale
        - non “torna indietro” oltre ts_backstep_tol_ms
        """
        if not self.ts_check_enabled:
            return True

        now_ms = int(time.time() * 1000)

        # 1) future drift
        if adj_ts_ms > now_ms + self.ts_future_slack_ms:
            drift = adj_ts_ms - now_ms
            log(f"[PacketHandler] timestamp future drift: adj_ts={adj_ts_ms}ms now={now_ms}ms drift=+{drift}ms slack={self.ts_future_slack_ms}ms", "WARNING")
            if self.ts_drop_on_violation:
                return False  # droppa questo report

        # 2) backstep
        if self._last_adj_ts_ms is not None:
            back = self._last_adj_ts_ms - adj_ts_ms
            if back > self.ts_backstep_tol_ms:
                log(f"[PacketHandler] timestamp backstep: adj_ts={adj_ts_ms}ms last={self._last_adj_ts_ms}ms step=-{back}ms tol={self.ts_backstep_tol_ms}ms", "WARNING")
                if self.ts_drop_on_violation:
                    return False

        # aggiorna “last” solo se non droppiamo
        self._last_adj_ts_ms = adj_ts_ms
        return True


    def __on_packet_parsing(self):
        """Worker: consuma dalla coda e applica pipeline a 4 passi"""
        log("[PacketHandler] Packet parsing started", "INFO")
        while self.running:
            try:
                element = self.queue.get(timeout=1)
            except Empty:
                continue
            try:
                self.__parse_data(element)
            except Exception as e:
                log(f"[PacketHandler] Error while parsing element: {e}", "ERROR")
                log(f">> {traceback.format_exc()}")
        # Stop
        self.thread = None
        log("[PacketHandler] Packet parsing stopped", "INFO")

    # ---------- parsing pipeline (public flow) ----------

    def __parse_data(self, element: list):
        """Accoda al buffer e estrae tutti i frame completi"""
        try:
            data_buffer, tStamp = element[0], element[1]
        except Exception:
            log("[PacketHandler] __parse_data: element empty?", "WARNING")
            return

        if not data_buffer:
            return

        # append e protezione contro crescita illimitata
        self.buffer += data_buffer
        if len(self.buffer) > self._max_buffer:
            keep = 2 * self.data_header_size
            self.buffer = self.buffer[-keep:]
            now = time.time()
            if (now - self._last_buffer_warn) > 5.0:
                self._last_buffer_warn = now
                log(f"[PacketHandler] buffer trimmed to {keep} bytes (cap reached)", "WARNING")

        # estrai tutti i frame presenti nel buffer
        while True:
            frame = self.__validate_data()  # compat: wrapper che chiama _validate_format_and_extract_frame()
            if not frame:
                break
            try:
                self.__handle_one_frame(frame)
            except Exception as e:
                log(f"[PacketHandler] Error handling one frame: {e}", "ERROR")
                log(f">> {traceback.format_exc()}")


    @staticmethod
    def crc32_ieee(data: ByteString) -> int:
        """CRC-32/IEEE 802.3 (reflected), polynomial 0xEDB88320; same as binascii.crc32."""
        return zlib.crc32(data) & 0xFFFFFFFF

    @staticmethod
    def crc32_header_without_crc(header: ByteString) -> int:
        """
        Compute CRC-32 (IEEE 802.3) over the header, excluding the trailing 4-byte CRC.
        Assumes the CRC field is a little-endian uint32 at the end of the header.
        """
        mv = memoryview(header)
        return PacketHandler.crc32_ieee(mv[:-4])


    # ---------- step 1: validate format (framing) ----------

    @staticmethod
    def _find_sync(buf: bytes, start: int = 0) -> int:
        """Trova la prima sync valida partendo da 'start'. Ritorna -1 se non trovata."""
        i = -1
        for sw in PacketHandler._SYNC_SET:
            j = buf.find(sw, start)
            if j != -1:
                i = j if i == -1 else min(i, j)
        return i

    def __validate_data(self):
        """(compat) chiama il nuovo validatore"""
        return self._validate_format_and_extract_frame()

    def _validate_format_and_extract_frame(self):
        """
        Framing header-first a dimensione fissa:
        - trova la prima sync (A55A o 5AA5)
        - se non c’è abbastanza per l’header, attende
        - calcola la lunghezza attesa: header + nreports(header)*report_size (o 0 se headerOnly)
        - se non ci sono ancora tutti i byte, attende
        - se ci sono byte in più, verifica la sync *al confine* del frame successivo;
          se il confine reale è spostato, logga 'payload len ... != expected ...' e si resincronizza.
        """
        buf = self.buffer
        if not buf:
            return None

        # helper: prima sync (A55A o 5AA5) a partire da 'start'
        def _find_any_sync(b: bytes, start: int = 0) -> int:
            i1 = b.find(b'\xA5\x5A', start)
            i2 = b.find(b'\x5A\xA5', start)
            cand = [i for i in (i1, i2) if i != -1]
            return min(cand) if cand else -1

        # 1) trova inizio frame
        i = _find_any_sync(buf, 0)
        if i == -1:
            if len(buf) > 1:
                self.buffer = buf[-1:]   # conserva un byte per sync spezzata
            return None

        # scarta garbage prima della sync
        if i > 0:
            self.buffer = buf = buf[i:]

        # 2) serve almeno l'header
        if len(buf) < self.data_header_size:
            return None

        header = bytes(buf[:self.data_header_size])

        # 2.bis) Verifica CRC dell'header (se abilitato e c'è spazio per 4B di CRC)
        if self.header_crc32_enabled:
            if self.data_header_size < 40:
                if not self._crc_cfg_warned:
                    log(f"[PacketHandler] header CRC enabled but headerlen={self.data_header_size} < 40 → skipping CRC check", "WARNING")
                    self._crc_cfg_warned = True
            else:
                try:
                    stored_crc   = int.from_bytes(header[-4:], "little")
                    computed_crc = PacketHandler.crc32_header_without_crc(header)
                    if stored_crc != computed_crc:
                        log(f"[PacketHandler] header CRC32 mismatch (stored=0x{stored_crc:08X}, computed=0x{computed_crc:08X})", 
                            "WARNING")
                        if self.header_crc32_strict:
                            # strict: resincronizza subito al prossimo preambolo valido
                            j = _find_any_sync(buf, 1)
                            if j == -1:
                                self.buffer = buf[-1:]
                            else:
                                self.buffer = buf[j:]
                            return None
                        else:
                            # compat: prosegui a parsare questo frame (loggato sopra)
                            pass
                except Exception as e:
                    log(f"[PacketHandler] header CRC check error: {e}", "ERROR")
                    # fallback sicuro: prova a riallinearti per non restare bloccato
                    j = _find_any_sync(buf, 1)
                    self.buffer = (buf[-1:] if j == -1 else buf[j:])
                    return None

        # --- preambolo: estrai nreports e headerOnly dai due byte di pre1/pre2
        h36  = memoryview(header)[:36]
        pre1 = h36[2]
        pre2 = h36[3]
        nreports_hdr = (pre1 & 0x0F)
        header_only  = ((pre2 >> 3) & 0x01) == 1

        # 3) lunghezza attesa del frame
        nrep = None
        if header_only:
            expected_payload = 0
        else:
            nrep = nreports_hdr if nreports_hdr > 0 else int(self.nreports_default)
            expected_payload = nrep * int(self.data_report_size)

        expected_total = int(self.data_header_size) + expected_payload

        # 4) se non ho ancora tutti i byte → attendo
        if len(buf) < expected_total:
            return None

        # 4.bis) se ho anche i 2 byte successivi, controlla sync *al confine*
        if len(buf) >= expected_total + 2:
            boundary = bytes(buf[expected_total:expected_total+2])
            if boundary not in self._SYNC_SET:
                # cerca prossima sync oltre l’inizio (evita la sync di partenza)
                j1 = _find_any_sync(buf, 2)
                if j1 != -1 and j1 != expected_total:
                    got = max(0, j1 - int(self.data_header_size))
                    exp = int(expected_total - int(self.data_header_size))

                    # classifica: undersize se la prossima sync è PRIMA del confine atteso; oversize se è DOPO
                    kind = "undersize" if j1 < expected_total else "oversize"
                    delta = got - exp
                    try:
                        rep_sz = int(self.data_report_size)
                        diff_reports = int(delta // rep_sz) if rep_sz else 0
                    except Exception:
                        diff_reports = 0

                    # log con contesto + throttle (chiave include anche 'kind' per non sopprimere over/under alternati)
                    import time
                    key = (exp, int(nrep) if nrep is not None else 0, int(header_only), kind)
                    now = time.monotonic()
                    last = self._mm_last.get(key, 0.0)
                    if now - last >= self._mm_min_interval:
                        nr_show = int(nrep) if nrep is not None else 0
                        log(
                            f"[PacketHandler] payload len {got} != expected {exp} "
                            f"({kind}, Δ={delta} bytes, ~{diff_reports:+d} reports) "
                            f"(nreports={nr_show}, header_only={int(header_only)}, "
                            f"expected_total={expected_total}, resync_at={j1})",
                            "WARNING"
                        )
                        self._mm_last[key] = now

                    # resincronizza scartando il frame corrotto
                    self.buffer = buf[j1:]
                    return None
                else:
                    # fallback: header probabilmente corrotto; cerca una sync più avanti e riallinea
                    j2 = _find_any_sync(buf, 1)  # da 1 perché vogliamo uscire da "non allineati"
                    if j2 == -1:
                        dropped = max(0, len(buf) - 1)
                        if dropped:
                            log(f"[PacketHandler] frame boundary desync: dropped {dropped} bytes before next sync", "WARNING")
                        self.buffer = buf[-1:]
                    else:
                        if j2 > 0:
                            log(f"[PacketHandler] frame boundary desync: dropped {j2} bytes before next sync", "WARNING")
                        self.buffer = buf[j2:]
                    return None


        # 5) estrai frame e avanza
        frame = bytes(buf[:expected_total])
        self.buffer = buf[expected_total:]

        # 6) sicurezza: se i prossimi 2 byte non sono una sync, prova a riallinearti
        if len(self.buffer) >= 2:
            nxt = bytes(self.buffer[:2])
            if nxt not in self._SYNC_SET:
                j3 = _find_any_sync(self.buffer, 1)
                if j3 == -1:
                    dropped = max(0, len(self.buffer) - 1)
                    if dropped:
                        log(f"[PacketHandler] frame boundary desync: dropped {dropped} bytes before next sync", "WARNING")
                    self.buffer = self.buffer[-1:]
                else:
                    if j3 > 0:
                        log(f"[PacketHandler] frame boundary desync: dropped {j3} bytes before next sync", "WARNING")
                    self.buffer = self.buffer[j3:]

        # 7) ritorna il frame completo
        return frame

    # ---------- step 2: parse header content ----------

    def _parse_header_content(self, header: bytes) -> HeaderInfo:
        """
        Header (36B + [optional 4B CRC]):
        - 2B sync, 2B preamble
        - 8B FFT timestamp (uint64 little-endian)
        - 6*4B metrics: [STA/LTA, MAX%, IQR, RMS, PEAK, CAV]
        - [optional] 4B CRC32 little-endian at the end (not part of the above fields)
        """
        if len(header) < 36:
            raise ValueError("[PacketHandler] Invalid header length")

        h36 = memoryview(header)[:36]   # usa sempre e solo i primi 36 byte per i campi

        # preamble
        pre1 = h36[2]
        pre2 = h36[3]

        flag_fft    = (pre1 & 0b10000000) >> 7
        flag_stalta = (pre1 & 0b01000000) >> 6
        data_format = (pre1 & 0b00110000) >> 4
        nreports    = (pre1 & 0b00001111)

        chmaxPerc      = (pre2 & 0b00000011)
        nChannelsFlag  = (pre2 & 0b00000100) >> 2
        headerOnlyMode = (pre2 & 0b00001000) >> 3

        # tStampFFT (8B) — nel file C è un uint64_t;
        # supponiamo little-endian come i delta nel payload
        tfft_bytes = h36[4:12]
        tStampFFT = int.from_bytes(tfft_bytes, byteorder="little", signed=False)

        metrics = [
            h36[12:16],  # STA/LTA
            h36[16:20],  # MAX%
            h36[20:24],  # IQR
            h36[24:28],  # RMS
            h36[28:32],  # PEAK
            h36[32:36],  # CAV
        ]

        return HeaderInfo(
            flag_stalta=flag_stalta,
            flag_fft=flag_fft,
            data_format=data_format,
            nreports=nreports,
            chmaxPerc=chmaxPerc,
            nChannelsFlag=nChannelsFlag,
            headerOnlyMode=headerOnlyMode,
            tStampFFT=tStampFFT,
            metrics=metrics
        )

    # ---------- step 3: evaluate alarms ----------

    def _evaluate_alarms(self, hdr: HeaderInfo, now_ms: int):
        """Aggiorna stato allarmi/trigger e gestisce finestra evento."""
        if hdr.flag_stalta == 1:
            if not self.alarm_state:
                log("[PacketHandler] STA/LTA flag entered in pre-alarm state", "INFO")
            self.alarm_state = True
            self.alarm_state_timestamp = now_ms
            if self.triggered_timestamp:
                # aggiorna 'last alarm ts' se esiste evento aperto
                for e in self.triggered_buffer_list:
                    if e[0] == self.triggered_timestamp:
                        e[1] = self.alarm_state_timestamp
                        break

        if self.alarm_state and (not self.triggered) and hdr.flag_fft == 1:
            log(f"[PacketHandler] Triggering alarm on channel {hdr.chmaxPerc}", "INFO")
            self.triggered = True
            self.triggered_timestamp = now_ms
            t_close = now_ms + self.EVENT_POST_MS
            # [t_trigger, last_alarm_ts, buffer(None finché non inizializzato), t_close]
            self.triggered_buffer_list.append([self.triggered_timestamp, self.alarm_state_timestamp, None, t_close])
            self.on_alarm_received(now_ms, 1)  # notifica minima

        # chiusura prealarm se 30s senza aggiornamenti
        if self.alarm_state and (now_ms - (self.alarm_state_timestamp or now_ms)) > 30000:
            log("[PacketHandler] Resetting alarms", "INFO")
            self.alarm_state = False
            self.triggered = False
            self.alarm_state_timestamp = None
            self.triggered_timestamp = None

        # chiusura eventi a tempo (anche se STALTA è sceso)
        if self.triggered_buffer_list:
            now_ms_local = now_ms
            to_remove = []
            for idx, e in enumerate(self.triggered_buffer_list):
                t_close = e[3]
                if now_ms_local >= t_close:
                    log("[PacketHandler] Sending triggered buffer to the alarm handler", "INFO")
                    # e[2] potrebbe essere stato inizializzato in __handle_alarm_event
                    self.on_alarm_received(e[0], None, e[2] or [])
                    to_remove.append(idx)
            for idx in reversed(to_remove):
                del self.triggered_buffer_list[idx]
                log("[PacketHandler] Removed event data from list", "DEBUG")
            if not self.triggered_buffer_list:
                # reset stato (evento chiuso)
                self.triggered = False
                self.triggered_timestamp = None

    # ---------- step 4: parse payload content ----------

    def _to_ms(self, ts_abs: int) -> int:
        """Converte un timestamp assoluto (header/payload) in millisecondi secondo SENSOR_TS_UNIT."""
        return int(ts_abs) if self.ts_unit == "ms" else int(ts_abs // 1000)

    def _parse_payload_content(self, payload: bytes, hdr: HeaderInfo):
        """Estrae i report dal payload interpretando il primo campo come timestamp UNIX assoluto (µs di default)."""
        nreports = hdr.nreports if hdr.nreports > 0 else int(self.nreports_default)
        expected = nreports * int(self.data_report_size)
        got = len(payload)
        if got != expected:
            log(f"[PacketHandler] payload len {got} != expected {expected} (nreports={nreports})", "WARNING")
            if got < expected:
                return  # undersize → droppa frame
            else:
                payload = payload[:expected]  # oversize → taglia

        frame_base_ms = None

        for k in range(nreports):
            start  = k * self.data_report_size
            report = payload[start: start + self.data_report_size]

            # 1) timestamp ASSOLUTO del campione (uint64, UNIX time) → ms
            sample_ts_abs = int.from_bytes(report[:8], byteorder='little', signed=False)
            adj_ts = self._to_ms(sample_ts_abs)
            if frame_base_ms is None:
                frame_base_ms = adj_ts

            # 2) Estrazione canali
            a, c, i, t, it = self.__get_channels_data(report[8:])

            # 3) sanity check temporale PRIMA del salvataggio se configurato per droppare in caso di violazione
            try:
                if not self._ts_check(int(adj_ts)):
                    if getattr(self, "ts_drop_on_violation", False):
                        log(f"[PacketHandler] timestamp sanity check failed (adj_ts={adj_ts}, k={k}/{nreports}) — dropping sample", "WARNING")
                        # puoi comunque notificare eventuali eventi legati al frame
                        self.__handle_alarm_event(adj_ts, a, it, hdr.metrics, report[:8])
                        continue
                    else:
                        log(f"[PacketHandler] timestamp sanity check failed (adj_ts={adj_ts}) — keeping due to config", "WARNING")
            except Exception as e:
                log(f"[PacketHandler] _ts_check error: {e}", "ERROR")

            # 4) salvataggio se ci sono accelerazioni
            if len(a) > 0:
                if self._last_saved_ts is not None and adj_ts < self._last_saved_ts:
                    # Con la correzione per frame questo dovrebbe sparire; teniamolo come guardrail.
                    log(f"[PacketHandler] skip save due to timestamp backstep: adj={adj_ts} < last={self._last_saved_ts}", "WARNING")
                else:
                    try:
                        self.storer.save(a + it, adj_ts, frame_base_ms if frame_base_ms is not None else adj_ts)
                        self._last_saved_ts = adj_ts
                    except Exception as e:
                        log(f"[PacketHandler] save() failed: adj_ts={adj_ts} frame_base_ms={frame_base_ms}", "ERROR")
                        log(f"[PacketHandler] Error while saving the data: {e}", "ERROR")

            # 5) Gestione eventi allarme
            self.__handle_alarm_event(adj_ts, a, it, hdr.metrics, report[:8])


    # ---------- per-frame orchestrator ----------

    def __handle_one_frame(self, frame: bytes):
        """Applica i 4 passaggi per il singolo frame"""
        header = frame[:self.data_header_size]
        payload = frame[self.data_header_size:]

        # 2) parse header
        hdr = self._parse_header_content(header)

        # Log compatibility with tests A1 and A2
        if not hasattr(self, "_alarm_state"):
            self._alarm_state = {"stalta": False, "fft": False}

        self.__log_alarm_flags(hdr.flag_stalta, hdr.flag_fft)

        # log compatibile coi test (P4)
        if hdr.headerOnlyMode == 0 and self.headerOnlyMode_default:
            log(f"[PacketHandler] Header parsed: nreports={hdr.nreports} headerOnly={hdr.headerOnlyMode} payload_len={len(payload)}", "INFO")
        
        # --- timestamp sanity (header)
        hdr_ts_ms = self._normalize_header_ts_ms(hdr.tStampFFT)
        if not self._guard_header_timestamp(hdr_ts_ms) and self.ts_header_drop_on_violation:
            # scarta frame in modalità “strict”
            return

        # warning “differisce da config”
        if hasattr(self, "nreports_default"):
            nr_def = int(self.nreports_default)
            if hdr.nreports not in (0, nr_def):
                log(f"[PacketHandler] Number of reports differs from configuration (header={hdr.nreports}, config={nr_def})", "WARNING")

        # 3) evaluate alarms
        now_ms = int(time.time() * 1000)
        self._evaluate_alarms(hdr, now_ms)

        # 4) parse payload (se non header-only)
        if hdr.headerOnlyMode == 0:
            self._parse_payload_content(payload, hdr)

    # ---------- helpers: eventi & canali ----------
    def __log_alarm_flags(self, stalta_flag: bool, fft_flag: bool) -> None:
        """
        Logga eventi di allarme STALTA/FFT solamente su transizione di stato.
        - RAISE: 0 -> 1
        - CLEAR: 1 -> 0
        Evita log ripetitivi a ogni frame quando il flag resta stabile.
        """
        # STALTA
        prev = self._alarm_state.get("stalta", False)
        cur  = bool(stalta_flag)
        if cur and not prev:
            log("[PacketHandler] Alarm STALTA: RAISE", "INFO")
        elif not cur and prev:
            log("[PacketHandler] Alarm STALTA: CLEAR", "INFO")
        self._alarm_state["stalta"] = cur

        # FFT
        prev = self._alarm_state.get("fft", False)
        cur  = bool(fft_flag)
        if cur and not prev:
            log("[PacketHandler] Alarm FFT: RAISE", "INFO")
        elif not cur and prev:
            log("[PacketHandler] Alarm FFT: CLEAR", "INFO")
        self._alarm_state["fft"] = cur


    def __handle_alarm_event(self, delta_timestamp, a, it, hMetrics, hTstamp: bytes):
        """Accumula dati per eventi: -30s .. +45s rispetto al trigger."""
        try:
            # eventi aperti
            if self.triggered_buffer_list:
                to_remove = []
                for j in range(len(self.triggered_buffer_list)):
                    # inizializza buffer evento con history (30s pre)
                    if self.triggered_buffer_list[j][2] is None:
                        log("[PacketHandler] Creating a new event data buffer", "INFO")
                        self.triggered_buffer_list[j][2] = [] + self.buffer_history
                    # aggiungi dato corrente
                    self.triggered_buffer_list[j][2].append(
                        (delta_timestamp, a + it + hMetrics + [hTstamp])
                    )
                    # chiudi quando si raggiunge la deadline t_close
                    t_close = self.triggered_buffer_list[j][3]
                    if int(time.time() * 1000) >= t_close:
                        log("[PacketHandler] Sending triggered buffer to the alarm handler", "INFO")
                        self.on_alarm_received(self.triggered_buffer_list[j][0], None, self.triggered_buffer_list[j][2])
                        to_remove.append(j)

                for j in sorted(to_remove, reverse=True):
                    del self.triggered_buffer_list[j]
                    log("[PacketHandler] Removed event data from list", "DEBUG")

            # history (30s)
            self.buffer_history.append((delta_timestamp, a + it + hMetrics + [hTstamp]))
            max_hist = max(1, 30 * self.frequency)
            if len(self.buffer_history) > max_hist:
                del self.buffer_history[0]
        except Exception as e:
            log(f"[PacketHandler] Error in handling triggered buffer: {e}", "ERROR")
            log(f">> {traceback.format_exc()}")

    def __get_channels_data(self, data: bytes):
        """
        Dati report (self.data_report_size - 8) = 32 + 12:
        - 8 canali raw (8 * 4B = 32)
        - 3 accelerometri filtrati (3 * 4B = 12) solo se presenti >=3 accelerometri
        """
        if len(data) != (self.data_report_size - 8):
            raise ValueError("[PacketHandler] __get_channels_data Invalid data length")

        raw_data = [data[k * 4: (k + 1) * 4] for k in range(8)]

        # mappa canali: es. '11140000'
        a, c, i, t, it = [], [], [], [], []
        for j in range(len(self.channels)):
            ch = self.channels[j]
            if ch == '1':
                a.append(raw_data[j])
            elif ch == '2':
                c.append(raw_data[j])
            elif ch == '3':
                i.append(raw_data[j])
            elif ch == '4':
                it.append(raw_data[j])
            elif ch == '5':
                t.append(raw_data[j])

        # filtered accel (se >=3 accelerometri)
        if self.channels.count('1') >= 3:
            f = [data[32 + k * 4: 32 + (k + 1) * 4] for k in range(3)]
        else:
            f = []

        return (f, c, i, t, it) if self.is_filtered else (a, c, i, t, it)
