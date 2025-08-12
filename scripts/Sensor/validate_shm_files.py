#!/usr/bin/env python3
"""
validate_shm_files.py

Validator per file SHM prodotti dal modulo Sensor.

Formato atteso del filename (finalizzato):
  shm_<MAC>_05_<t0_us>_<t1_us>

Righe file (testuali, esadecimali):
  <deltaT> <ax> <ay> <az> <temp>
  - deltaT: uint32 big-endian (8 cifre hex), microsecondi dal primo campione (t0)
  - ax, ay, az, temp: float32 big-endian (8 cifre hex ciascuno)

Uscita:
  - Codice 0 se tutto OK
  - Codice 1 se solo warning
  - Codice 2 se errori

Uso:
  python tools/validate_shm_files.py path1 [path2 ...]
  Opzioni: vedi --help
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

FILENAME_RE = re.compile(
    r'^shm_([0-9A-Fa-f]{12})_05_(\d{10,20})_(\d{10,20})$'
)

LINE_TOKEN_RE = re.compile(r'\s+')

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Validator per file SHM (timestamp µs, deltaT u32 BE, valori float32 BE).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument('paths', nargs='+', help='File o directory (ricorsivo) da validare')
    p.add_argument('--tolerance-us', type=int, default=0,
                   help='Tolleranza sul confronto t1 = t0 + ultimo_deltaT (in µs)')
    p.add_argument('--acc-range', type=float, nargs=2, metavar=('MIN', 'MAX'),
                   default=None, help='Range plausibile per accelerometri (g) per warning')
    p.add_argument('--temp-range', type=float, nargs=2, metavar=('MIN', 'MAX'),
                   default=None, help='Range plausibile per temperatura (°C) per warning')
    p.add_argument('--pattern', default='shm_*_05_*_*', help='Glob per selezionare i file (se path è directory)')
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

def decode_u32_be_hex(token: str) -> Tuple[int, str]:
    token = token.strip()
    if len(token) != 8:
        return -1, f"deltaT non ha 8 cifre hex (len={len(token)})"
    try:
        return int(token, 16), ""
    except ValueError:
        return -1, "deltaT non è esadecimale valido"

def decode_f32_be_hex(token: str) -> Tuple[float, str]:
    token = token.strip()
    if len(token) != 8:
        return float('nan'), f"float32 non ha 8 cifre hex (len={len(token)})"
    try:
        b = bytes.fromhex(token)
        val = struct.unpack('!f', b)[0]
        return val, ""
    except Exception as e:
        return float('nan'), f"float32 non valido: {e}"

def validate_file(path: Path, tol_us: int, acc_range, temp_range) -> Dict[str, Any]:
    name = path.name
    m = FILENAME_RE.match(name)
    result: Dict[str, Any] = {
        "file": str(path),
        "ok": True,
        "warnings": [],
        "errors": [],
        "stats": {
            "lines": 0,
            "bad_lines": 0,
            "max_delta_us": 0,
            "first_line_delta_us": None,
            "last_line_delta_us": None,
            "t0_us": None,
            "t1_us": None
        }
    }
    if not m:
        result["errors"].append("Filename non conforme: atteso shm_<MAC12>_05_<t0>_<t1>")
        result["ok"] = False
        return result

    mac_hex, t0_s, t1_s = m.groups()
    try:
        t0 = int(t0_s)
        t1 = int(t1_s)
    except ValueError:
        result["errors"].append("t0/t1 non numerici")
        result["ok"] = False
        return result
    result["stats"]["t0_us"] = t0
    result["stats"]["t1_us"] = t1

    last_delta = None
    with path.open('rt', encoding='utf-8', errors='replace') as fh:
        for idx, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            result["stats"]["lines"] += 1
            tokens = re.split(r'\s+', line.strip())
            if len(tokens) < 5:
                result["errors"].append(f"Linea {idx}: attesi 5 token, trovati {len(tokens)}")
                result["stats"]["bad_lines"] += 1
                result["ok"] = False
                continue

            delta_us, err = decode_u32_be_hex(tokens[0])
            if err:
                result["errors"].append(f"Linea {idx}: {err}")
                result["stats"]["bad_lines"] += 1
                result["ok"] = False
                continue

            if delta_us < 0 or delta_us > 0xFFFFFFFF:
                result["errors"].append(f"Linea {idx}: deltaT fuori range uint32 ({delta_us})")
                result["stats"]["bad_lines"] += 1
                result["ok"] = False
                continue

            if result["stats"]["first_line_delta_us"] is None:
                result["stats"]["first_line_delta_us"] = delta_us
            result["stats"]["last_line_delta_us"] = delta_us
            if delta_us > result["stats"]["max_delta_us"]:
                result["stats"]["max_delta_us"] = delta_us

            if last_delta is not None and delta_us < last_delta:
                result["errors"].append(f"Linea {idx}: deltaT non monotono ({delta_us} < {last_delta})")
                result["ok"] = False
            last_delta = delta_us

            vals = []
            for j in range(1, 5):
                v, ferr = decode_f32_be_hex(tokens[j])
                if ferr:
                    result["errors"].append(f"Linea {idx}: {ferr}")
                    result["stats"]["bad_lines"] += 1
                    result["ok"] = False
                    break
                vals.append(v)
            if len(vals) == 4:
                ax, ay, az, temp = vals
                if acc_range is not None:
                    mn, mx = acc_range
                    for comp, label in [(ax,'ax'), (ay,'ay'), (az,'az')]:
                        if not math.isfinite(comp):
                            result["errors"].append(f"Linea {idx}: {label} non finito (NaN/Inf)")
                            result["ok"] = False
                        elif comp < mn or comp > mx:
                            result["warnings"].append(f"Linea {idx}: {label} fuori range {mn}..{mx}: {comp}")
                if temp_range is not None:
                    mn, mx = temp_range
                    if not math.isfinite(temp):
                        result["errors"].append(f"Linea {idx}: temperatura non finita (NaN/Inf)")
                        result["ok"] = False
                    elif temp < mn or temp > mx:
                        result["warnings"].append(f"Linea {idx}: temperatura fuori range {mn}..{mx}: {temp}")

    last = result["stats"]["last_line_delta_us"]
    if last is None:
        result["warnings"].append("File vuoto o senza righe valide")
    else:
        expected_t1 = t0 + last
        if abs(expected_t1 - t1) > tol_us:
            result["errors"].append(f"t1 nel filename ({t1}) non combacia con t0+last_delta ({expected_t1}) ±{tol_us}µs")
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
        lines = d["stats"]["lines"]
        print(f" - {fname}: {status} (righe={lines})")
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
