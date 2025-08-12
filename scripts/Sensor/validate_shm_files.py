#!/usr/bin/env python3
"""
validate_shm_files.py  (BINARIO)

Validator per file SHM prodotti dallo Storer (formato binario).

Filename finale atteso:
  shm_<MAC>_05_<t0_us>_<t1_us>.dat

Record binario (20 B ciascuno):
  <deltaT:uint32 LE> <ax:float32 LE> <ay:float32 LE> <az:float32 LE> <temp:float32 LE>
  - deltaT: microsecondi dal primo campione (t0)
  - ax, ay, az, temp: float32 LE

Uscita:
  - Codice 0 se tutto OK
  - Codice 1 se solo warning
  - Codice 2 se errori
"""
import argparse
import os
import re
import sys
import struct
import math
import json
from pathlib import Path
from typing import List, Dict, Any, Tuple

# shm_<MAC12>_05_<t0>_<t1>.dat  (t0/t1 in µs)
FILENAME_RE = re.compile(
    r'^shm_([0-9A-Fa-f]{12})_05_(\d{10,20})_(\d{10,20})\.dat$'
)

RECORD_SIZE = 4 + 4*4  # 20 bytes
RECORD_STRUCT = struct.Struct('<Iffff')

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Validator per file SHM binari (deltaT u32 LE, valori float32 LE).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument('paths', nargs='+', help='File o directory (ricorsivo) da validare')
    p.add_argument('--tolerance-us', type=int, default=0,
                   help='Tolleranza sul confronto t1 = t0 + ultimo_deltaT (in µs)')
    p.add_argument('--acc-range', type=float, nargs=2, metavar=('MIN', 'MAX'),
                   default=None, help='Range plausibile per accelerometri (g) per warning')
    p.add_argument('--temp-range', type=float, nargs=2, metavar=('MIN', 'MAX'),
                   default=None, help='Range plausibile per temperatura (°C) per warning')
    p.add_argument('--pattern', default='shm_*_05_*_*.dat',
                   help='Glob per selezionare i file (se path è directory)')
    p.add_argument('--json-out', type=str, default=None, help='Scrive un report JSON nel percorso indicato')
    p.add_argument('--strict', action='store_true', help='Tratta i warning come errori (exit code 2)')
    return p.parse_args()

def iter_candidate_files(paths: List[str], pattern: str) -> List[Path]:
    files: List[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_file():
            files.append(pp)
        elif pp.is_dir():
            files.extend(pp.rglob(pattern))
        else:
            print(f"[WARN] Percorso non trovato: {p}", file=sys.stderr)
    files = [f for f in files if f.is_file()]
    return sorted(files, key=lambda x: str(x))

def validate_file(path: Path, tol_us: int, acc_range, temp_range) -> Dict[str, Any]:
    name = path.name
    m = FILENAME_RE.match(name)
    result: Dict[str, Any] = {
        "file": str(path),
        "ok": True,
        "warnings": [],
        "errors": [],
        "stats": {
            "records": 0,
            "bad_records": 0,
            "max_delta_us": 0,
            "first_delta_us": None,
            "last_delta_us": None,
            "t0_us": None,
            "t1_us": None,
            "filesize": path.stat().st_size
        }
    }

    if not m:
        result["errors"].append("Filename non conforme: atteso shm_<MAC12>_05_<t0>_<t1>.dat")
        result["ok"] = False
        return result

    mac_hex, t0_s, t1_s = m.groups()
    try:
        t0 = int(t0_s); t1 = int(t1_s)
    except ValueError:
        result["errors"].append("t0/t1 non numerici")
        result["ok"] = False
        return result
    result["stats"]["t0_us"] = t0
    result["stats"]["t1_us"] = t1

    size = result["stats"]["filesize"]
    if size == 0:
        result["warnings"].append("File vuoto (0 bytes)")
        return result
    if size % RECORD_SIZE != 0:
        result["errors"].append(f"Dimensione file non multipla di {RECORD_SIZE} B (size={size})")
        result["ok"] = False
        # Proviamo comunque a leggere le parti intere, segnando come bad il trailing
        readable_bytes = size - (size % RECORD_SIZE)
    else:
        readable_bytes = size

    last_delta = None

    with path.open('rb') as fh:
        # Lettura a blocchi per efficienza
        # (ma RECORD_SIZE è piccolo: va bene anche record‑per‑record)
        idx = 0
        while idx * RECORD_SIZE < readable_bytes:
            buf = fh.read(RECORD_SIZE)
            if not buf:
                break
            if len(buf) != RECORD_SIZE:
                result["errors"].append(f"Record troncato a offset {idx*RECORD_SIZE} (len={len(buf)})")
                result["stats"]["bad_records"] += 1
                result["ok"] = False
                break

            try:
                delta_us, ax, ay, az, temp = RECORD_STRUCT.unpack(buf)
            except Exception as e:
                result["errors"].append(f"Record {idx}: unpack fallito ({e})")
                result["stats"]["bad_records"] += 1
                result["ok"] = False
                break

            # Statistiche
            if result["stats"]["first_delta_us"] is None:
                result["stats"]["first_delta_us"] = delta_us
            result["stats"]["last_delta_us"] = delta_us
            if delta_us > result["stats"]["max_delta_us"]:
                result["stats"]["max_delta_us"] = delta_us
            result["stats"]["records"] += 1

            # deltaT monotono
            if last_delta is not None and delta_us < last_delta:
                result["errors"].append(f"Record {idx}: deltaT non monotono ({delta_us} < {last_delta})")
                result["ok"] = False
            last_delta = delta_us

            # Controlli valori
            # (NaN/Inf e range se richiesto)
            comps = [(ax, 'ax'), (ay, 'ay'), (az, 'az')]
            for v, label in comps:
                if not math.isfinite(v):
                    result["errors"].append(f"Record {idx}: {label} non finito (NaN/Inf)")
                    result["ok"] = False
                elif acc_range is not None:
                    mn, mx = acc_range
                    if v < mn or v > mx:
                        result["warnings"].append(f"Record {idx}: {label} fuori range {mn}..{mx}: {v}")

            if not math.isfinite(temp):
                result["errors"].append(f"Record {idx}: temperatura non finita (NaN/Inf)")
                result["ok"] = False
            elif temp_range is not None:
                mn, mx = temp_range
                if temp < mn or temp > mx:
                    result["warnings"].append(f"Record {idx}: temperatura fuori range {mn}..{mx}: {temp}")

            idx += 1

        # bytes residui (se size non multiplo)
        leftover = size - readable_bytes
        if leftover:
            result["errors"].append(f"Byte residui non multipli di {RECORD_SIZE}: {leftover} B")
            result["ok"] = False

    # Coerenza t1 filename
    last = result["stats"]["last_delta_us"]
    if last is None:
        result["warnings"].append("File senza record validi")
    else:
        expected_t1 = t0 + last
        if abs(expected_t1 - t1) > tol_us:
            result["errors"].append(
                f"t1 nel filename ({t1}) != t0+last_delta ({expected_t1}) ±{tol_us}µs"
            )
            result["ok"] = False

    return result

def main() -> int:
    args = parse_args()
    files = iter_candidate_files(args.paths, args.pattern)
    if not files:
        print("[ERROR] Nessun file da validare", file=sys.stderr)
        return 2

    summary: Dict[str, Any] = {
        "files_total": 0,
        "files_ok": 0,
        "files_warn": 0,
        "files_error": 0,
        "details": []
    }

    for f in files:
        res = validate_file(f, args.tolerance_us, args.acc_range, args.temp_range)
        summary["files_total"] += 1
        if res["ok"] and not res["warnings"]:
            summary["files_ok"] += 1
        elif res["ok"] and res["warnings"]:
            summary["files_warn"] += 1
        else:
            summary["files_error"] += 1
        summary["details"].append(res)

    print(f"[SUMMARY] tot={summary['files_total']} ok={summary['files_ok']} warn={summary['files_warn']} err={summary['files_error']}")
    for d in summary["details"]:
        status = "OK"
        if not d["ok"]:
            status = "ERROR"
        elif d["warnings"]:
            status = "WARN"
        fname = Path(d["file"]).name
        recs = d["stats"]["records"]
        print(f" - {fname}: {status} (records={recs})")
        if d["errors"]:
            for e in d["errors"][:5]:
                print(f"    * {e}")
            if len(d["errors"]) > 5:
                print(f"    * ... (+{len(d['errors'])-5} errori)")
        if d["warnings"]:
            for w in d["warnings"][:5]:
                print(f"    ~ {w}")
            if len(d["warnings"]) > 5:
                print(f"    ~ ... (+{len(d['warnings'])-5} warning)")

    if args.json_out:
        try:
            outp = Path(args.json_out)
            outp.parent.mkdir(parents=True, exist_ok=True)
            outp.write_text(json.dumps(summary, indent=2), encoding='utf-8')
            print(f"[INFO] Report JSON scritto in: {outp}")
        except Exception as e:
            print(f"[WARN] Impossibile scrivere JSON: {e}", file=sys.stderr)

    if summary["files_error"] > 0:
        return 2
    if args.strict and summary["files_warn"] > 0:
        return 2
    if summary["files_warn"] > 0:
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
