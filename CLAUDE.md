# Branch imx8mp-webrtc-voice: agent loop rules (do not upstream this file)

Follow AGENTS.md first. This file only adds the loop for this branch.

## Goal
Voice capture on the i.MX8MP HiFi4 under IPC3, enabled out of the box:
micfil DAI (48 kHz) → webrtc_hpf → webrtc_aec (ref = playback demux) →
SRC 48→16 kHz → webrtc_ns → Voice PCM. Raw PCM stays unprocessed.
Reuse the upstream webrtc_* modules; write integration only (Kconfig,
topology1 m4, IPC3 glue), never new DSP algorithms.

## Definition of done
`scripts/voice-loop/voice-loop.sh` exits 0. Gates live in
`scripts/voice-loop/voice-loop.env`:
- NS-only stage (P2): `GATES=nr,xrun`
- AEC stage (P3+): `GATES=erle,nr,xrun` (ERLE >= 25 dB, NR >= 10 dB, 0 xruns)

## Loop
1. Make the smallest change toward the current phase.
2. Run `voice-loop.sh` (or `build` alone while it does not compile).
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
- AEC backend AECm (`CONFIG_WEBRTC_AEC_BACKEND_AECM=y`, set explicitly).
- Built in (`=y`), no LLEXT (IPC3).
- No kcontrols in v1 (webrtc switch controls are IPC4-only).

## Never
- push, force-push, or rewrite pushed history
- add Signed-off-by
- change files outside this feature without saying why
