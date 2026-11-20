#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Score one voice-loop run against the gates.

Two modes, same windows (layout.json from gen_stimulus.py):

bench  Testbench run. The clean near-end signal is known, so near-end
       preservation is measured exactly: the output is aligned to the
       clean near-end, the best-fit gain g and the residual r = y - g*n
       are computed per window.
         near_gain_db = 20*log10(g)            (near-end level kept)
         near_sdr_db  = 10*log10(|g*n|^2/|r|^2) (how clean it is)
       Reference for ERLE/NR is the synthetic mic input.

evk    Board run. Reference is the Raw PCM recorded at the same time.
       Near-end preservation is the level loss Raw->Voice in the
       near-only window; double talk cannot be separated acoustically
       and is left to the bench mode.

Metrics (all in dB; power ratios do not depend on sample rate):
  erle       input/output power, far-only window after convergence
  nr         input/output power, noise-only window
  near_loss  input/output power, near-only window       (lower is better)
  dt_gain    near-end gain during double talk  (bench)  (closer to 0 is better)
  dt_sdr     near-end SDR during double talk   (bench)
  xruns      overrun/underrun/xrun lines in the logs

Prints one human line and one JSON line; exit 0 = PASS, 1 = FAIL.
"""
import argparse
import json
import re
import sys
import wave

import numpy as np

GATES = {  # name: (metric, comparison, default threshold)
    "erle": ("erle", ">=", 25.0),
    "nr": ("nr", ">=", 10.0),
    "near": ("near_loss", "<=", 3.0),
    "dt": ("dt_gain", ">=", -6.0),
    "dtsdr": ("dt_sdr", ">=", 12.0),
    "xrun": ("xruns", "<=", 0),
    # skeleton stage (stub backends): Voice must equal Raw in level and shape
    "passlvl": ("pass_level_db", "<=", 1.0),
    "passcorr": ("pass_corr", ">=", 0.9),
}


def load(path, rate=None, raw_ch=1):
    """WAV, or headerless S16/S32 .raw (needs rate). Returns (x, rate)."""
    if path.endswith(".raw"):
        x = np.fromfile(path, np.int16).astype(np.float64) / 32768.0
        return x.reshape(-1, raw_ch)[:, 0], rate
    with wave.open(path, "rb") as w:
        r, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        b = w.readframes(w.getnframes())
    dt = {2: np.int16, 4: np.int32}[width]
    x = np.frombuffer(b, dt).reshape(-1, ch)[:, 0].astype(np.float64)
    return x / float(np.iinfo(dt).max), r


def resample(x, r_in, r_out):
    if r_in == r_out:
        return x
    n_out = int(round(len(x) * r_out / r_in))
    spec = np.fft.rfft(x)
    keep = n_out // 2 + 1
    spec = spec[:keep] if keep <= len(spec) else np.pad(spec, (0, keep - len(spec)))
    return np.fft.irfft(spec, n_out) * (n_out / len(x))


def win(x, rate, t0, t1):
    s = x[int(t0 * rate):int(t1 * rate)]
    if len(s) == 0:
        raise SystemExit(f"window {t0:.1f}-{t1:.1f}s is outside the recording")
    return s


def pw(x):
    return float(np.mean(x ** 2)) + 1e-12


def ratio_db(ref, out, rr, ro, t0, t1):
    return 10 * np.log10(pw(win(ref, rr, t0, t1)) / pw(win(out, ro, t0, t1)))


def align(out, ref, max_lag):
    """Lag (samples) that best aligns ref to out, ref leading."""
    n = 1 << int(np.ceil(np.log2(len(out) + len(ref))))
    c = np.fft.irfft(np.fft.rfft(out, n) * np.conj(np.fft.rfft(ref, n)), n)
    return int(np.argmax(c[:max_lag]))


def near_fit(out, near, rate, t0, t1):
    y, n = win(out, rate, t0, t1), win(near, rate, t0, t1)
    m = min(len(y), len(n))
    y, n = y[:m], n[:m]
    g = float(np.dot(y, n) / (np.dot(n, n) + 1e-12))
    r = y - g * n
    return 20 * np.log10(max(abs(g), 1e-6)), 10 * np.log10(pw(g * n) / pw(r))


def count_xruns(paths):
    pat = re.compile(r"overrun|underrun|xrun", re.IGNORECASE)
    n = 0
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                n += sum(1 for line in f if pat.search(line))
        except FileNotFoundError:
            pass
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["bench", "evk"])
    ap.add_argument("ref", help="bench: mic_bench.wav; evk: raw.wav")
    ap.add_argument("out", help="processed output (Voice PCM / testbench output)")
    ap.add_argument("layout")
    ap.add_argument("--near-clean", help="bench: near_clean.wav")
    ap.add_argument("--out-rate", type=int, default=16000, help="rate of a .raw output")
    ap.add_argument("--gates", default="nr,near,xrun",
                    help="comma list: " + ",".join(GATES))
    ap.add_argument("--set", action="append", default=[], metavar="GATE=VALUE",
                    help="override a threshold, e.g. --set erle=20")
    ap.add_argument("--converge", type=float, default=4.0)
    ap.add_argument("--margin", type=float, default=0.5)
    ap.add_argument("--max-delay", type=float, default=0.3,
                    help="max processing delay searched when aligning (s)")
    ap.add_argument("--logs", nargs="*", default=[])
    a = ap.parse_args()

    with open(a.layout) as f:
        lay = json.load(f)
    ref, rr = load(a.ref)
    out, ro = load(a.out, a.out_rate)
    m = a.margin
    fo, nt, no, nz = lay["far_only"], lay["dt"], lay["near_only"], lay["noise_only"]

    res = {"mode": a.mode}
    if a.mode == "bench":
        near, rn = load(a.near_clean)
        near = resample(near, rn, ro)
        lag = align(out, near, int(a.max_delay * ro))
        out_al = out[lag:]
        ref_al = resample(ref, rr, ro)
        res["delay_ms"] = round(1000 * lag / ro, 1)
        res["erle"] = ratio_db(ref_al, out_al, ro, ro, fo[0] + a.converge, fo[1] - m)
        res["nr"] = ratio_db(ref_al, out_al, ro, ro, nz[0] + m, nz[1] - m)
        res["near_loss"] = ratio_db(near, out_al, ro, ro, no[0] + m, no[1] - m)
        res["dt_gain"], res["dt_sdr"] = near_fit(out_al, near, ro, nt[0] + m, nt[1] - m)
    else:
        res["erle"] = ratio_db(ref, out, rr, ro, fo[0] + a.converge, fo[1] - m)
        res["nr"] = ratio_db(ref, out, rr, ro, nz[0] + m, nz[1] - m)
        res["near_loss"] = ratio_db(ref, out, rr, ro, no[0] + m, no[1] - m)
        # Pass-through check for the skeleton stage: over the far-end span,
        # Voice must match Raw in level and waveform (after rate + delay).
        span = (lay["far"][0] + m, lay["far"][1] - m)
        raw_r = resample(ref, rr, ro)
        lag = align(out, raw_r, int(a.max_delay * ro))
        y, x = win(out[lag:], ro, *span), win(raw_r, ro, *span)
        k = min(len(y), len(x))
        y, x = y[:k], x[:k]
        res["pass_level_db"] = abs(10 * np.log10(pw(x) / pw(y)))
        res["pass_corr"] = float(np.dot(x, y) / np.sqrt(pw(x) * pw(y)) / k)
    res["xruns"] = count_xruns(a.logs)

    thr = {k: v[2] for k, v in GATES.items()}
    for s in a.set:
        k, v = s.split("=")
        thr[k] = float(v)
    fails, skipped = [], []
    gates = [g.strip() for g in a.gates.split(",") if g.strip()]
    for g in gates:
        metric, op, _ = GATES[g]
        if metric not in res:
            skipped.append(g)
            continue
        v = res[metric]
        ok = v >= thr[g] if op == ">=" else v <= thr[g]
        if not ok:
            fails.append(f"{metric} {v:.1f} {'<' if op == '>=' else '>'} {thr[g]}")

    for k in list(res):
        if isinstance(res[k], float):
            res[k] = round(res[k], 2)
    verdict = "FAIL" if fails else "PASS"
    shown = ", ".join(f"{k} {v}" for k, v in res.items() if k != "mode")
    print(f"{verdict} [{a.mode}]: {shown}"
          + (f" | fails: {'; '.join(fails)}" if fails else "")
          + (f" | skipped (no data in this mode): {','.join(skipped)}" if skipped else ""))
    print(json.dumps({"verdict": verdict, **res, "gates": gates,
                      "thresholds": {g: thr[g] for g in gates},
                      "fails": fails, "skipped": skipped}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
