# voice-loop: build → deploy → measure for the i.MX8MP voice chain

Closed loop for developing the HiFi4 voice capture chain on the i.MX8MP EVK
(`webrtc_aec` → SRC → `webrtc_ns`, IPC3). One command builds firmware and
topology, deploys them to the board, plays a far-end stimulus while recording
the Raw and Voice PCMs, and prints PASS/FAIL against the gates.

Runs on the development host. Python here is only the test harness; all
audio processing runs in C on the HiFi4.

## Files

| File | Role |
| --- | --- |
| `voice-loop.sh` | Stages `build`, `deploy`, `measure`, `all` (default) |
| `gen_stimulus.py` | 48 kHz stereo stimulus: silence, far-end, silence (+ layout JSON) |
| `voice_metrics.py` | ERLE / NR / xrun scoring and gates; exit 0 = PASS |
| `voice-loop.env.example` | Settings template; copy to `voice-loop.env` |

## Setup

1. Passwordless ssh to the EVK (`ssh-copy-id root@imx8mp-evk`).
2. `cp voice-loop.env.example voice-loop.env` and set `EVK_HOST`, the PCMs
   and the build paths.
3. Host needs `python3` with `numpy`, `cmake`, `ninja`, `alsa-utils`
   (for `alsatplg`), and the imx8m firmware toolchain.

## Run

```sh
scripts/voice-loop/voice-loop.sh            # all stages
scripts/voice-loop/voice-loop.sh measure    # re-measure without rebuilding
cat scripts/voice-loop/out/latest/metrics.json
```

## Metrics

- **ERLE** = 10·log10(P_raw / P_voice) over the far-end window, after a 4 s
  convergence time. Gate: ≥ 25 dB. Measured on the full chain output, so
  the residual noise floor caps it (a synthetic 30 dB echo cut with 12 dB NR
  reads ~23.5 dB).
- **NR** = the same ratio over the trailing silence (ambient noise only).
  Gate: ≥ 10 dB. Run a fan or play room noise from a second device for a
  meaningful number.
- **xrun** = lines matching overrun/underrun/xrun in the aplay/arecord logs
  and dmesg.

Gates per phase: NS-only (P2) `GATES=nr,xrun`; with AEC (P3+)
`GATES=erle,nr,xrun`.
