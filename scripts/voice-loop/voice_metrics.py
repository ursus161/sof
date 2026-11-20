#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Score one voice-loop run: ERLE, NR and xruns, against the gates.

Inputs are the Raw (unprocessed mic) and Voice (processed) captures that
were recorded at the same time, plus the stimulus layout JSON. Power
ratios do not depend on sample rate, so Raw (48 kHz) and Voice (16 kHz)
are compared window by window without resampling.

  ERLE = 10*log10(P_raw / P_voice) over the far-end window, after the
         AEC convergence time.
  NR   = 10*log10(P_raw / P_voice) over the trailing silence window
         (ambient noise only).

Prints one human line and one JSON line; exits 0 on PASS, 1 on FAIL.
"""
import argparse
import json
import re
import sys
import wave

import numpy as np


def load(path):
    with wave.open(path, "rb") as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    dtype = {2: np.int16, 4: np.int32}[width]
    data = np.frombuffer(raw, dtype).reshape(-1, ch)[:, 0].astype(np.float64)
    return data / float(np.iinfo(dtype).max), rate


def power(x, rate, t0, t1):
    seg = x[int(t0 * rate):int(t1 * rate)]
    if len(seg) == 0:
        raise SystemExit(f"window {t0}-{t1}s is outside the recording")
    return float(np.mean(seg ** 2)) + 1e-12


def count_xruns(log_paths):
    pat = re.compile(r"overrun|underrun|xrun", re.IGNORECASE)
    n = 0
    for p in log_paths:
        try:
            with open(p, errors="replace") as f:
                n += sum(1 for line in f if pat.search(line))
        except FileNotFoundError:
            pass
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw")
    ap.add_argument("voice")
    ap.add_argument("layout")
    ap.add_argument("--gates", default="nr",
                    help="comma list of gates to enforce: erle,nr,xrun")
    ap.add_argument("--erle-min", type=float, default=25.0)
    ap.add_argument("--nr-min", type=float, default=10.0)
    ap.add_argument("--converge", type=float, default=4.0,
                    help="seconds of far-end ignored while the AEC converges")
    ap.add_argument("--margin", type=float, default=0.5,
                    help="seconds trimmed at window edges (path delay)")
    ap.add_argument("--logs", nargs="*", default=[],
                    help="aplay/arecord/dmesg logs scanned for xruns")
    args = ap.parse_args()

    with open(args.layout) as f:
        lay = json.load(f)
    raw, r_rate = load(args.raw)
    voice, v_rate = load(args.voice)

    e0, e1 = lay["far_start"] + args.converge, lay["far_end"] - args.margin
    n0, n1 = lay["far_end"] + args.margin, lay["end"] - args.margin

    erle = 10 * np.log10(power(raw, r_rate, e0, e1) / power(voice, v_rate, e0, e1))
    nr = 10 * np.log10(power(raw, r_rate, n0, n1) / power(voice, v_rate, n0, n1))
    xruns = count_xruns(args.logs)

    gates = {g.strip() for g in args.gates.split(",") if g.strip()}
    fails = []
    if "erle" in gates and erle < args.erle_min:
        fails.append(f"ERLE {erle:.1f} < {args.erle_min}")
    if "nr" in gates and nr < args.nr_min:
        fails.append(f"NR {nr:.1f} < {args.nr_min}")
    if "xrun" in gates and xruns:
        fails.append(f"{xruns} xrun lines")

    verdict = "FAIL" if fails else "PASS"
    print(f"{verdict}: ERLE {erle:.1f} dB, NR {nr:.1f} dB, xruns {xruns}"
          + (f" ({'; '.join(fails)})" if fails else ""))
    print(json.dumps({"verdict": verdict, "erle_db": round(erle, 2),
                      "nr_db": round(nr, 2), "xruns": xruns,
                      "gates": sorted(gates), "fails": fails}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
