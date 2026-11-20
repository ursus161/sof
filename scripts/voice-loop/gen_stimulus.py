#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Generate the far-end stimulus for voice-loop.sh.

Layout (seconds), written as 48 kHz stereo S16_LE:
  [0, LEAD)            silence        - lets pipelines settle
  [LEAD, LEAD+FAR)     far-end signal - echo for the AEC (ERLE window)
  [LEAD+FAR, END)      silence        - ambient noise only (NR window)

The far-end signal is a user-supplied WAV (music/speech, recommended) or,
by default, speech-shaped noise bursts so the loop runs with no assets.
The layout is written next to the WAV as JSON so voice_metrics.py knows
the windows.
"""
import argparse
import json
import wave

import numpy as np

RATE = 48000


def speech_shaped_bursts(seconds, rng):
    """Pink-ish noise, amplitude-modulated in ~250 ms syllable bursts."""
    n = int(seconds * RATE)
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    shape = 1 / np.sqrt(np.maximum(freqs, 100.0))
    shape[freqs > 7000] *= 0.1
    pink = np.fft.irfft(spec * shape, n)
    env = (np.sin(2 * np.pi * 4 * np.arange(n) / RATE) > -0.3).astype(float)
    sig = pink * env
    return 0.3 * sig / np.max(np.abs(sig))


def read_far_end(path, seconds):
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 48 kHz S16 WAV")
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        data = data.reshape(-1, w.getnchannels())[:, 0] / 32768.0
    n = int(seconds * RATE)
    reps = int(np.ceil(n / len(data)))
    return np.tile(data, reps)[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="output WAV path")
    ap.add_argument("--far-end", help="48 kHz S16 WAV to use as far-end signal")
    ap.add_argument("--lead", type=float, default=2.0)
    ap.add_argument("--far", type=float, default=12.0)
    ap.add_argument("--tail", type=float, default=6.0)
    args = ap.parse_args()

    rng = np.random.default_rng(1)
    far = (read_far_end(args.far_end, args.far) if args.far_end
           else speech_shaped_bursts(args.far, rng))
    sig = np.concatenate([np.zeros(int(args.lead * RATE)), far,
                          np.zeros(int(args.tail * RATE))])
    pcm = np.clip(sig * 32767, -32768, 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)

    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(stereo.tobytes())

    layout = {"far_start": args.lead, "far_end": args.lead + args.far,
              "end": args.lead + args.far + args.tail}
    with open(args.out + ".json", "w") as f:
        json.dump(layout, f)


if __name__ == "__main__":
    main()
