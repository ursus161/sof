#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Generate the test signals for voice-loop.sh.

One timeline, five windows (default lengths in seconds):

  lead (2)     silence                  pipelines settle
  far  (12)    far-end only             ERLE: echo must disappear
  dt   (6)     far-end + near-end       double talk: near-end must survive
  near (6)     near-end only            near-end must pass unharmed
  noise (6)    neither                  NR: ambient noise must drop

Outputs in OUT_DIR:
  far.wav        48 kHz stereo S16, played by the EVK speaker (echo source)
  near.wav       48 kHz mono S16, near-end talker on the same timeline;
                 played by a second device on the EVK run (NEAR_PLAY_CMD)
  mic_bench.wav  48 kHz mono S16 synthetic microphone for the testbench:
                 far-end through a room impulse response + near + noise
  near_clean.wav near-end exactly as mixed into mic_bench.wav (reference)
  layout.json    window boundaries, read by voice_metrics.py
  *.raw          headerless copies of the WAVs for the SOF testbench

Default signals are synthetic so the loop runs with no assets; pass real
recordings with --far-wav / --near-wav / --ir-wav for meaningful scores.
"""
import argparse
import json
import os
import wave

import numpy as np

RATE = 48000


def write_wav(path, x, channels=1):
    pcm = np.clip(x * 32767, -32768, 32767).astype(np.int16)
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    pcm.tofile(os.path.splitext(path)[0] + ".raw")


def read_wav(path, seconds):
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 48 kHz S16 WAV")
        d = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        d = d.reshape(-1, w.getnchannels())[:, 0] / 32768.0
    n = int(seconds * RATE)
    return np.tile(d, int(np.ceil(n / len(d))))[:n]


def shaped_noise(n, rng, tilt=0.5, hi=7000.0):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / RATE)
    spec *= 1 / np.maximum(f, 100.0) ** tilt
    spec[f > hi] *= 0.1
    x = np.fft.irfft(spec, n)
    return x / np.max(np.abs(x))


def talker(seconds, rng, rate_hz):
    """Speech-like: shaped noise gated in syllable-rate bursts."""
    n = int(seconds * RATE)
    env = (np.sin(2 * np.pi * rate_hz * np.arange(n) / RATE
                  + rng.uniform(0, 6.28)) > -0.2).astype(float)
    return 0.3 * shaped_noise(n, rng) * env


def synthetic_ir(rng, rt60=0.25, delay_ms=3.0):
    """Exponentially decaying room response with a direct-path delay."""
    n = int(rt60 * RATE)
    ir = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    d = int(delay_ms * RATE / 1000)
    ir = np.concatenate([np.zeros(d), [1.0], 0.3 * ir])
    return 0.5 * ir / np.sqrt(np.sum(ir ** 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--far-wav", help="48 kHz S16 far-end recording (music/speech)")
    ap.add_argument("--near-wav", help="48 kHz S16 near-end speech recording")
    ap.add_argument("--ir-wav", help="48 kHz speaker-to-mic impulse response")
    ap.add_argument("--lead", type=float, default=2.0)
    ap.add_argument("--far", type=float, default=12.0)
    ap.add_argument("--dt", type=float, default=6.0)
    ap.add_argument("--near", type=float, default=6.0)
    ap.add_argument("--noise", type=float, default=6.0)
    ap.add_argument("--noise-db", type=float, default=-45.0,
                    help="testbench ambient noise level, dBFS")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    rng = np.random.default_rng(1)

    t = np.cumsum([0, a.lead, a.far, a.dt, a.near, a.noise])
    lay = {"far": [t[1], t[3]], "far_only": [t[1], t[2]], "dt": [t[2], t[3]],
           "near_only": [t[3], t[4]], "noise_only": [t[4], t[5]], "end": t[5]}
    n = int(t[5] * RATE)

    def place(sig, start):
        x = np.zeros(n)
        i = int(start * RATE)
        x[i:i + len(sig)] = sig[:n - i]
        return x

    far_sig = read_wav(a.far_wav, a.far + a.dt) if a.far_wav else talker(a.far + a.dt, rng, 3.0)
    near_sig = read_wav(a.near_wav, a.dt + a.near) if a.near_wav else talker(a.dt + a.near, rng, 4.5)
    far = place(far_sig, lay["far"][0])
    near = place(near_sig, lay["dt"][0])

    ir = read_wav(a.ir_wav, 0.5) if a.ir_wav else synthetic_ir(rng)
    echo = np.convolve(far, ir)[:n]
    noise = 10 ** (a.noise_db / 20) * shaped_noise(n, rng, tilt=1.0)
    mic = echo + near + noise

    write_wav(os.path.join(a.out_dir, "far.wav"), far, channels=2)
    write_wav(os.path.join(a.out_dir, "near.wav"), near)
    write_wav(os.path.join(a.out_dir, "mic_bench.wav"), mic)
    write_wav(os.path.join(a.out_dir, "near_clean.wav"), near)
    with open(os.path.join(a.out_dir, "layout.json"), "w") as f:
        json.dump({k: (list(map(float, v)) if isinstance(v, list) else float(v))
                   for k, v in lay.items()}, f, indent=1)


if __name__ == "__main__":
    main()
