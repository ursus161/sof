# Branch imx8mp-webrtc-voice: agent loop rules (do not upstream this file)

Follow AGENTS.md first. This file only adds the loop for this branch.

## Goal
Voice capture on the i.MX8MP HiFi4 under IPC3, enabled out of the box:
micfil DAI (48 kHz) → webrtc_hpf → webrtc_aec (ref = playback demux) →
SRC 48→16 kHz → webrtc_ns → Voice PCM. Raw PCM stays unprocessed.
Reuse the upstream webrtc_* modules; write integration only (Kconfig,
topology1 m4, host-library build of the webrtc modules + topology2 test topology for the bench, IPC3 glue), never new DSP algorithms.

## Definition of done
`scripts/voice-loop/voice-loop.sh` exits 0 (see scripts/voice-loop/README.md).
- `bench` (host testbench, deterministic) must pass before any board run:
  `BENCH_GATES=erle,nr,near,dt,dtsdr`
- `measure` (EVK, acoustic):
  - NS-only stage (P2): `GATES=nr,near,xrun`
  - AEC stage (P3+): `GATES=erle,nr,near,xrun`
Thresholds: ERLE >= 25 dB, NR >= 10 dB, near-end loss <= 3 dB,
double-talk near gain >= -6 dB and SDR >= 12 dB, 0 xruns.
Never relax a threshold to get a PASS; report instead.

## Loop
1. Make the smallest change toward the current phase.
2. Run `voice-loop.sh build` until it compiles, then `bench`, then the board.
3. Read `scripts/voice-loop/out/latest/metrics.json` and the logs next to it.
4. On FAIL: fix and repeat. Stop and report after 5 failed iterations,
   or on any deploy error you cannot explain from dmesg.
5. On PASS: commit (AGENTS.md subject format, no Signed-off-by) and move
   to the next to-do of the phase.

## Fixed decisions (do not revisit without new measurements)
- Topology mirrors the IPC3 Chromebook AEC topology
  (`sof-mt8195-mt6359-rt1019-rt5682.m4`): volume + demux playback,
  10 ms periods, AEC ref buffer wired to the demux; clone
  `google_rtc_audio_processing.m4` for webrtc_aec (documented drop-in).
- AEC backend: Float, WebRTC classic float AEC (`CONFIG_WEBRTC_AEC_BACKEND_FLOAT=y`, the upstream
  default for `=y`; uses the 8MP FPU). AECm is the fallback, AEC3 the
  candidate (its CMake hardcodes a toolchain path: fix upstream first);
  switching needs P4 numbers from both stages.
- Built in (`=y`), no LLEXT (IPC3).
- No kcontrols in v1 (webrtc switch controls are IPC4-only).
- The 10 ms playback period applies only to the AEC topology variant.

## Never
- push, force-push, or rewrite pushed history
- add Signed-off-by
- change files outside this feature without saying why
