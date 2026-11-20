# voice-loop: closed loop for the i.MX8MP voice chain

Develop and verify the HiFi4 voice capture chain on the i.MX8MP
(micfil → `webrtc_hpf` → `webrtc_aec` → SRC 48→16 kHz → `webrtc_ns`, IPC3)
with one command that answers PASS or FAIL.

Python here is only the test harness and runs on the development host. All
audio processing runs in C: in the SOF firmware on the DSP, or in the SOF
host testbench, which compiles the same C modules for the PC.

## Two stages, two kinds of evidence

| Stage | Runs | Proves | Cannot prove |
| --- | --- | --- | --- |
| `bench` | SOF host testbench (IPC4 library, topology2 webrtc classes) on WAV files | Algorithm quality, exactly and repeatably: echo removed, noise removed, near-end talker kept, also in double talk. Fast, CI-friendly | DSP timing, real acoustics |
| `measure` | The EVK with real speaker and mics | Real echo path, real noise, xruns, end-to-end integration | Exact near-end preservation in double talk |

`all` runs build → bench → deploy → measure and stops at the first FAIL.

## Signals (gen_stimulus.py)

One timeline, five windows:

| Window | Content | What is checked |
| --- | --- | --- |
| lead (2 s) | silence | — |
| far-only (12 s) | far-end only | ERLE after 4 s convergence |
| dt (6 s) | far-end + near-end | double talk: near-end gain and SDR (bench) |
| near-only (6 s) | near-end only | near-end level loss |
| noise-only (6 s) | ambient noise | NR |

Synthetic by default (speech-shaped bursts, synthetic room response);
pass real recordings with `STIM_ARGS` for meaningful numbers.

## Gates (voice_metrics.py)

| Gate | Metric | Threshold | Catches |
| --- | --- | --- | --- |
| `erle` | echo power in / out, far-only window | ≥ 25 dB | echo left in |
| `nr` | noise power in / out, noise-only window | ≥ 10 dB | noise left in |
| `near` | near-end level loss, near-only window | ≤ 3 dB | a chain that mutes everything |
| `dt` | near-end gain during double talk (bench) | ≥ −6 dB | an AEC that ducks the talker |
| `dtsdr` | near-end signal-to-distortion in double talk (bench) | ≥ 12 dB | echo or artefacts over the talker |
| `xrun` | overrun/underrun/xrun log lines | 0 | real-time failures |

Self-test with simulated processors (synthetic signals): a good AEC+NS
passes; a muting "cheater", a pass-through and a 12 dB talker-ducking AEC
all fail. ERLE and NR are measured on the full chain output, so on the
board the residual noise floor caps ERLE.

## Files

| File | Role |
| --- | --- |
| `voice-loop.sh` | Stages `build`, `bench`, `deploy`, `measure`, `all` |
| `gen_stimulus.py` | Far-end, near-end and synthetic mic signals + layout |
| `voice_metrics.py` | Scores a run in `bench` or `evk` mode; exit 0 = PASS |
| `voice-loop.env.example` | Settings template; copy to `voice-loop.env` |

## Setup

1. Passwordless ssh to the EVK (`ssh-copy-id root@<evk>`).
2. `cp voice-loop.env.example voice-loop.env`; set `EVK_HOST`, the PCMs,
   the build paths, and optionally `NEAR_PLAY_CMD` (a second speaker next
   to the EVK mics, driven from this host).
3. Host needs `python3` + `numpy`, `cmake`, `ninja`, `alsa-utils`
   (`alsatplg`), the imx8m firmware toolchain, and for `bench` the host
   testbench (`scripts/rebuild-testbench.sh`, IPC4 library) with the webrtc
   modules enabled, plus a topology2 test topology for the chain
   (`BENCH_TPLG`). The webrtc modules do not build in the host library yet:
   their CMake uses Zephyr-only calls (P1 task).

## Run

```sh
scripts/voice-loop/voice-loop.sh            # all stages
scripts/voice-loop/voice-loop.sh bench      # deterministic stage only
scripts/voice-loop/voice-loop.sh measure    # re-measure on the board
cat scripts/voice-loop/out/latest/metrics.json
```
