# imx8mp voice chain (AEC + NS on the HiFi4): local dev plan

> **Dev only, do not upstream.** Drop this file with the other `DNM:` and
> `scripts/voice-loop` commits before any upstream PR.
> Live version of the design doc (diagrams, comments):
> https://claude.ai/artifact/H8TP4E1HFqzhM8Li8xVKyB
> Snapshot: 20 Nov 2026, SOF main `d9b1a78`.

Contents:

1. [Next step: the architecture skeleton](#1-next-step-the-architecture-skeleton)
2. [Plan](#2-plan)
3. [Evidence and reference](#3-evidence-and-reference)
4. [IPC4 variant](#4-ipc4-variant)
5. [Glossary](#5-glossary)

---

## 1. Next step: the architecture skeleton

Goal: the whole topology running on the EVK with the upstream **stub**
backends, so routing, rates, channels and timing are proven before any real
algorithm. Swapping a stub for the real algorithm later is one Kconfig line.

### Step 1: board config (P0)

- `app/boards/imx8mp_evk_mimx8ml8_adsp.conf`:
  - `CONFIG_COMP_WEBRTC_AEC=y` + `CONFIG_COMP_WEBRTC_AEC_STUB=y`
  - `CONFIG_COMP_WEBRTC_NS=y` + `CONFIG_COMP_WEBRTC_NS_STUB=y`
  - `CONFIG_COMP_WEBRTC_HPF=y` + `CONFIG_COMP_WEBRTC_HPF_STUB=y`
  - check demux (`COMP_MUX`) and `COMP_SRC` are on
- No WebRTC source and no C++ are needed with stubs.
- Build with XCC (`scripts/xtensa-build-zephyr.py imx8m`); record image size and heap.
- **Gate:** firmware boots on the EVK.

### Step 2: component macros

- `tools/topology/topology1/m4/webrtc_aec.m4`, `webrtc_ns.m4`, `webrtc_hpf.m4`
- Clone `google_rtc_audio_processing.m4` (`webrtc_aec` is a documented drop-in
  for it); UUIDs from `uuid-registry.txt`.

### Step 3: capture pipe

- `tools/topology/topology1/sof/pipe-webrtc-voice-capture.m4`, modeled on
  `pipe-google-rtc-audio-processing-capture.m4`:

  ```
  host (Voice) <- NS <- SRC 48->16 <- AEC (+ ref buffer) <- HPF <- demux <- micfil DAI
                                                                     \-> host (Raw)
  ```

- The capture demux feeds the **Raw PCM**: on IPC3 one micfil DAI feeds one
  pipeline, so Raw is split inside the capture pipeline.
- 10 ms period.

### Step 4: top-level topology

- `tools/topology/topology1/sof-imx8mp-wm8960-aec.m4`:
  - playback from `sof-imx8-wm8960` (SAI3), switched to
    `pipe-volume-demux-playback.m4` at 10 ms;
  - capture: micfil DAI (as in `sof-imx8mp-micfil.m4`) + the new pipe;
  - SectionGraph: playback demux -> AEC reference input (as MT8195 does).
- Targets `sof-imx8mp-wm8960-aec` and `-wm8962-aec` in
  `tools/topology/topology1/CMakeLists.txt`.

### Step 5: verify on the EVK

- `scripts/voice-loop/voice-loop.sh` with `GATES=passlvl,passcorr,xrun`:
  Voice equals Raw (level within 1 dB, correlation >= 0.9), no xruns.
- Trace shows `webrtc_aec: mic_src=<n> ref_src=<m>` with a valid reference.
- SOF probes on the AEC reference input show the reference stream (stubs ignore it).

---

## 2. Plan

**In short:** echo cancellation (AEC) and noise suppression (NS) for the
microphone run on the i.MX8MP's HiFi4 DSP inside SOF, so Linux and the M7 do
no audio work. We don't write the algorithms: SOF main already ships WebRTC
modules for exactly this. The work is integration on imx8m under IPC3:

- build config;
- an IPC3 topology with a software echo reference;
- a test loop that proves quality;
- upstreaming.

### Goal

| Metric | Target |
| --- | --- |
| A53 / M7 load from AEC+NS | 0% (only the normal capture copy) |
| DSP load, mono, 16 kHz | <= 25% of the HiFi4 at 800 MHz |
| Echo removed (ERLE) | >= 25 dB |
| Noise removed (NR) | >= 10 dB on stationary noise |
| Near-end talker kept | <= 3 dB loss alone; >= -6 dB gain and >= 12 dB SDR in double talk |
| Out of box | Stock EVK image exposes a clean "Voice" PCM, no user config |

### Architecture

Linux configures and reads; the HiFi4 does all the processing; the M7 is untouched.

```
Linux (A53):  Playback PCM          SOF driver (IPC3 + topology1)         Voice PCM (16 kHz), Raw PCM
                   |                                                            ^
HiFi4 (SOF):  host -> volume -> demux -> SAI DAI -> codec + speaker            |
                                  |                                            |
                                  | echo reference, 48 kHz                     |
                                  v                                            |
              micfil DAI -> HPF -> webrtc_aec -> SRC 48->16 -> webrtc_ns -> host
              (PDM mics, 48 kHz)                     later: webrtc_vad, KPB -> KWD
```

| Component | Runs on | Role | Our work |
| --- | --- | --- | --- |
| Playback / Voice / Raw PCMs | A53 | ALSA endpoints; Voice at 16 kHz | Voice + Raw PCM in the topology |
| SOF driver + topology1 | A53 | Loads `sof-imx8m.ri` + `.tplg`, IPC3 | Topology name in the machine entry |
| Playback pipeline (10 ms) | HiFi4 | host -> volume -> demux -> SAI -> codec | Switch to `pipe-volume-demux-playback.m4` |
| Capture pipeline (10 ms) | HiFi4 | micfil 48 kHz -> HPF -> `webrtc_aec` -> SRC 48->16 -> `webrtc_ns` -> host | m4 macros cloned from the Google AEC ones |
| `webrtc_*` modules | HiFi4 | The algorithms | Reuse; board config, tuning |
| `webrtc_vad`, KPB -> KWD | HiFi4 | Later | - |

### Decisions

Each one holds unless the "revisit if" column happens.

| # | Decision | Why | Revisit if |
| --- | --- | --- | --- |
| 0 | Architecture first with the upstream stub backends | Same components, pins and UUIDs, so the topology is final from day one; a real algorithm is one Kconfig line later | - |
| 1 | Reuse the upstream `webrtc_*` modules; no new algorithm code | Merged and tested upstream; our risk is the platform | - |
| 2 | Topology = the Chromebook MT8195 IPC3 AEC pattern: volume + demux playback, 10 ms periods, reference from the demux | Proven on IPC3; `webrtc_aec` is a documented drop-in for Google's AEC | Maintainers only accept topology2 |
| 3 | Mic: micfil (`fsl_micfil`), 48 kHz, 1 channel | Halves the AEC cost | A beamformer is added |
| 4 | Chain: HPF -> AEC (48 kHz in, 16 kHz inside) -> SRC -> NS (16 kHz) -> Voice | AEC resamples itself; NS accepts only 8/16 kHz | - |
| 5 | Echo reference: software tap from the playback demux, after volume | EVK codecs have no feedback path; same clock, so a fixed delay | Hardware with amp feedback |
| 6 | AEC backend: Float (WebRTC classic float AEC); AECm fallback; AEC3 candidate | Upstream default for `=y`; the 8MP DSP has an FPU. Float and AECm are both WebRTC legacy; AEC3 is the maintained one, but its build needs an upstream fix first (hardcoded toolchain path, C++ on the imx8m toolchain) | P5 numbers |
| 7 | Built in (`=y`), no LLEXT | IPC3 can't load modules at runtime | IPC4 lands on i.MX |
| 8 | LL timing: 10 ms periods, each call < 0.5 ms | IPC3 has no DP domain | Cycle measurements exceed it |
| 9 | No kcontrols in v1 | webrtc switch controls are IPC4-only | P5 patch lands |
| 10 | Voice + Raw PCMs; the 10 ms playback period only in the AEC topology variant | Others keep the raw mic and low latency | - |
| 11 | Verification first; thresholds frozen | A chain must not pass by muting | Retuned once on real recordings |
| 12 | IPC3 topology work minimal; IPC4 is the long-term home | topology1 is legacy | NXP SOF maintainers decide otherwise |

### Verification

The gates measure what the listener hears, not only how much the output
drops. A chain that mutes everything passes ERLE and NR, but fails the
near-end gates. Code: `scripts/voice-loop/`.

| Stage | Where | Proves | Cannot prove |
| --- | --- | --- | --- |
| `bench` | SOF host testbench (IPC4 library, upstream topology2 webrtc classes), files in and out | Exact quality: echo and noise removed, talker kept, also in double talk. Repeatable, CI-ready | DSP timing, real room acoustics |
| `measure` | The EVK with real speaker and mics | Real echo path and noise, xruns, end-to-end integration | Exact double-talk quality |
| cycles | `xt-run` with XCC, or SOF trace on the board | Cycles per call against the 1 ms tick | - |

| Gate | Measures | Threshold | Catches |
| --- | --- | --- | --- |
| `passlvl` | Voice vs Raw level (skeleton stage) | <= 1 dB | a chain that isn't a clean pass-through |
| `passcorr` | Voice vs Raw waveform correlation (skeleton stage) | >= 0.9 | wrong routing, channel or corruption |
| `erle` | echo in/out, far-only window | >= 25 dB | echo left in |
| `nr` | noise in/out, noise-only window | >= 10 dB | noise left in |
| `near` | talker level loss, near-only window | <= 3 dB | a chain that mutes everything |
| `dt` | talker gain in double talk (bench) | >= -6 dB | an AEC that ducks the talker |
| `dtsdr` | talker signal-to-distortion in double talk (bench) | >= 12 dB | echo or artefacts over the talker |
| `xrun` | overrun/underrun log lines | 0 | real-time failures |

Test timeline: far-end only 12 s -> double talk 6 s -> near-end only 6 s -> noise only 6 s.

Harness self-test: a good AEC+NS passes; a muting chain, a pass-through and a
12 dB talker-ducking AEC all fail. Thresholds are first estimates: tune once
on real recordings, then freeze.

### Roadmap (about 10-14 weeks part-time, estimate)

| Phase | Work | Gate |
| --- | --- | --- |
| P0 · Build with stubs (1 wk) | Board config, stub backends, imx8m build | Firmware boots with the stub modules |
| P1 · Skeleton topology (2-3 wk) | Whole topology: playback demux, reference path, micfil -> HPF -> AEC -> SRC -> NS -> Voice + Raw | Voice equals Raw (passlvl, passcorr), no xruns in 24 h |
| P2 · Out of box, stubs (1-2 wk) | Machine-table topology name, UCM, sof-bin + Yocto | Stock image exposes Voice (pass-through) + Raw |
| P3 · Real NS (1-2 wk) | Flip `webrtc_ns` stub -> real | Board nr, near, xrun |
| P4 · Real AEC (2-3 wk) | Host-library port + bench; flip `webrtc_aec` stub -> Float; bulk delay | Bench erle, nr, near, dt, dtsdr; board erle, nr, near, xrun |
| P5 · Backend pick + upstream (2-3 wk) | Float vs AECm vs AEC3; IPC3 controls; PR series | Merged; stock image gives clean voice |

#### To-do

**P0 · Build with stubs**

- [x] Source audit of SOF main (section 3)
- [ ] Board conf: `CONFIG_COMP_WEBRTC_{AEC,NS,HPF}=y` with `_STUB=y`; build `imx8m` (Zephyr, IPC3)
- [ ] Boot on the EVK; record image size and heap use
- [ ] Add an imx8m IPC3 webrtc (stub) build to CI

**P1 · Skeleton topology (the whole architecture)**

- [ ] m4: `webrtc_aec.m4`, `webrtc_ns.m4`, `webrtc_hpf.m4` cloned from `google_rtc_audio_processing.m4`, UUIDs from `uuid-registry.txt`
- [ ] Playback: `pipe-volume-demux-playback.m4` at a 10 ms period; demux routes one channel to the reference
- [ ] Capture: micfil -> HPF -> AEC (ref from the demux, SectionGraph as on MT8195) -> SRC 48->16 -> NS -> Voice PCM; Raw PCM kept
- [ ] Targets `sof-imx8mp-wm8960-aec` / `-wm8962-aec` in `tools/topology/topology1/CMakeLists.txt`
- [ ] Trace shows `webrtc_aec: mic_src=… ref_src=…` with a valid ref; probes show the reference stream
- [ ] `measure` green on passlvl, passcorr, xrun; cycles per call; 24 h soak

**P2 · Out of box, still with stubs**

- [ ] Topology name in the `imx8.c` machine table
- [ ] UCM Voice verb; `sof-bin` + Yocto packaging
- [ ] Stock EVK image exposes Voice (pass-through) + Raw PCMs with no user config

**P3 · Real NS**

- [ ] Flip `webrtc_ns` stub -> real (west module `webrtc-apm`); heap and MCPS on imx8m
- [ ] `measure` green on nr, near, xrun

**P4 · Real AEC**

- [ ] Host-library CMake port of the webrtc modules; `sof-testbench4` + topology2 test topology (`BENCH_TPLG`)
- [ ] Record the EVK impulse response and far/near speech WAVs (`STIM_ARGS`)
- [ ] Flip `webrtc_aec` stub -> Float; `bench` green on erle, nr, near, dt, dtsdr
- [ ] Measure the bulk delay; second speaker via `NEAR_PLAY_CMD`; `measure` green on erle, nr, near, xrun

**P5 · Backend pick + upstream**

- [ ] Float vs AECm vs AEC3 (AEC3 after its upstream build fix): bench gates + board ERLE and MCPS; record here
- [ ] IPC3 switch-control handling in `webrtc_aec`, `_hpf`, `_agc`
- [ ] PR series: SOF, kernel, `alsa-ucm-conf` (tooling commits dropped)

**Later**

- [ ] `webrtc_vad` + KPB -> keyword detection
- [ ] IPC4 once the i.MX IPC4 PR merges (section 4)

### Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Modules never built or run on IPC3 / imx8m before | P0 build + CI coverage; fixes go upstream |
| Heap the overlay asks for doesn't fit the imx8m layout | Measure in P0; AECm fallback needs ~12 KB/ch |
| Software reference misses codec/speaker nonlinearity | Residual echo suppression + NS; board ERLE in P4 |
| 10 ms playback period adds latency | AEC topology variant only; low-latency one stays |
| webrtc modules don't build in the host testbench yet (Zephyr-only CMake) | Make their CMake library-safe (upstreamable); fallback: board-only measure |
| Host-to-EVK start skew for the near-end talker | 0.5 s window margins; exact double talk only in bench |
| Thresholds are first guesses | Tune once on real recordings, then freeze |

- [ ] micfil DAI at 1 channel in topology1, or 2 channels plus a demux matrix to mono?
- [ ] Where the AEC bulk delay is set (module config vs reference buffer depth)
- [ ] Free memory on imx8m with the chain loaded
- [ ] Host-library build of the webrtc modules for the testbench; mixed-rate outputs (`-R` applies to all outputs)
- [ ] Which topology format would the NXP SOF maintainers merge for this?
- [x] IPC3 support of the modules: data path yes; switch controls IPC4-only
- [x] Default mic: micfil (`fsl_micfil`)
- [x] IPC: IPC3 now; IPC4 follows its PR

---

## 3. Evidence and reference

### What already exists in SOF

SOF main carries a WebRTC processing suite. Its algorithms come from a west
module (`webrtc-apm` = `thesofproject/webrtc-audio-processing`, `sof-hifi`
branch: AEC3 C++17 core, AECm, NS, AGC) ([west.yml](https://github.com/thesofproject/sof/blob/main/west.yml)).

| Module | What it is | State |
| --- | --- | --- |
| `webrtc_aec` | 2-input AEC (mic + reference); Float, AECm and AEC3 backends + stub | Merged; IPC3 supported with pipeline-based mic/ref selection ([PR #11442](https://github.com/thesofproject/sof/pull/11442)) |
| `webrtc_ns` | Classic WebRTC NS, fixed-point Wiener filter, 8/16 kHz | Upstream suite ([PR #11314](https://github.com/thesofproject/sof/pull/11314)) |
| `webrtc_ns2` | RNNoise NS, 48 kHz | Upstream suite ([PR #10990](https://github.com/thesofproject/sof/pull/10990)) |
| `webrtc_vad`, `webrtc_agc`, `webrtc_hpf` | VAD, AGC, high-pass | Stubs by default with `CONFIG_COMP_STUBS`; real backends via `app/webrtc_overlay.conf` ([PR #11419](https://github.com/thesofproject/sof/pull/11419)) |

Reported load on an Intel PTL DSP at 400 MHz ([PR #10990](https://github.com/thesofproject/sof/pull/10990), `webrtc_aec/README.md`):

- AECm mono, 10 ms: ~12-16.5 MCPS end to end (Kconfig help: ~1.5 MCPS core)
- Float mono: ~42 MCPS core, ~55 end to end
- AEC3 mono: ~148 MCPS core, ~182 end to end
- NS at 16 kHz: ~4.2-6; RNNoise NS at 48 kHz: ~22-28.5; AGC ~1.2-1.6; HPF ~0.2-0.3

Why reuse wins:

- **Reviewed and tested upstream:** `sof-test` has functional tests for the modules ([sof-test #1365](https://github.com/thesofproject/sof-test/pull/1365)).
- **The hard component work is done:** the two-pin AEC, its mic/ref selection, and the stub/real backend pattern.
- **Our risk is the platform:** the IPC3 topology, the echo reference, memory and LL timing.
- **AECm is legacy in WebRTC,** which calls AEC3 the better alternative ([commit](https://webrtc.googlesource.com/src//+/c8096273bee01b8acaba43ff335c6c25658e4dab)).

### Source audit (SOF main)

| Finding in the source | Consequence |
| --- | --- |
| `webrtc_aec` accepts 48 kHz and resamples to 16 kHz internally for every backend; accepts S16 and S32; mic and ref must have the same rate | No SRC before the AEC; both pipelines at 48 kHz; micfil S32 is fine |
| On IPC3 the AEC treats the input whose pipeline ID matches its output's as the mic | The reference must come from another pipeline (playback demux) |
| Backends: Float (WebRTC classic float AEC, `WebRtcAec_*` from webrtc-audio-processing 0.3.x, uses the FPU), AECm (Q15), AEC3 (C++17, `=y` only); `=y` defaults to Float | Float is our default; AECm fallback, AEC3 candidate |
| Stub backends for AEC, NS, HPF, AGC (`COMP_WEBRTC_*_STUB`); the AEC stub copies mic to output | Architecture-first skeleton with no WebRTC source |
| `webrtc_ns` accepts only 8/16 kHz; AGC and VAD 8-48 kHz | SRC 48->16 kHz between AEC and NS |
| Switch controls in `webrtc_aec`, `_hpf`, `_agc` decode IPC4 payloads only | No enable switches in v1; IPC3 controls are a P5 patch |
| No webrtc modules in `app/stub_build_all_ipc3.conf` or the imx8mp board conf | Nobody builds them for IPC3 yet; add CI with our series |
| `app/webrtc_overlay.conf` sizes the heap (`HEAP_MEM_POOL_SIZE=131072`, `SOF_ZEPHYR_HEAP_SIZE=0x80000`) | Check the imx8m memory map can provide it |
| topology1 Chromebook MT8195 wires `pipe-volume-demux-playback.m4` + `pipe-google-rtc-audio-processing-capture.m4`, 10 ms periods, AEC ref from the demux | `webrtc_aec` is a documented drop-in: clone those m4 files |
| imx8mp topology1 targets `sof-imx8mp-micfil` and `sof-imx8mp-wm8960-kwd` exist | Start from them; keyword detection already runs on imx8mp |
| Topology2 classes `webrtc-*.conf`, `micsel.conf`, `muxdemux.conf` exist | The IPC4 variant reuses them directly |
| Testbench runs several pipelines with separate files (`-p 1,2 -i a,b -o c,d`); `-R`/`-n` apply to all outputs | A deterministic bench stage is feasible |
| The host testbench library is IPC4 (`library_defconfig`) with no webrtc modules; their CMake uses Zephyr-only calls | Bench needs a CMake port of the modules to the host library; run on `sof-testbench4` with topology2 |
| AEC3 CMake hardcodes `/home/lrg/zephyr-sdk-1.0.1/...` and targets Zephyr SDK GCC; imx8m builds with XCC | AEC3 needs an upstream build fix before it can be measured on imx8m |

### Platform facts

| Item | Value | Source |
| --- | --- | --- |
| Audio DSP | Cadence HiFi4, 800 MHz, with an FPU | [AN13970](https://Community.freescale.com/docs/en/application-note/AN13970.pdf), [NXP support](https://community.nxp.com/t5/Multi-Source-Translation-Content/2070325-en-US/ta-p/2242029) |
| On-chip RAM | OCRAM_A 256 KB (AUDIOMIX); OCRAM 576 KB, OCRAM_S 36 KB (SUPERMIX) | [datasheet](https://www.nxp.com/docs/en/preview/PREVIEW_IMX8MPIEC.pdf) |
| DSP working memory | Mostly a reserved DDR region, cached | [NXP community](https://community.nxp.com/t5/i-MX-Processors/Memory-size-of-HiFi4-Core-on-i-MX-8M-Plus/m-p/1464557) |
| Audio I/O | SAI (I2S/TDM), 8-channel PDM via micfil | [NXP product page](https://nxp.com/products/IMX8MPLUS) |
| SOF on imx8m | Zephyr only, native Zephyr EDMA/SDMA/SAI/MICFIL drivers; XTOS removed Oct 2026 | [PR #11312](https://github.com/thesofproject/sof/pull/11312) |
| Official imx8m build | Cadence XCC `RI-2023.11`, core `hifi4_mscale_v2_0_2_prod` | `scripts/xtensa-build-zephyr.py` |
| IPC | IPC3 today; IPC4 for i.MX is an open PR | - |
| Loadable modules | LLEXT is IPC4-only | [SOF LLEXT docs](https://thesofproject.github.io/latest/developer_guides/firmware/llext_modules.html) |

### Compute and optimization path

No new compute library is needed for v1. Optimize only where a measurement points:

1. **Configuration first:** backend, filter length (32/64/128 ms), NS level.
2. **HiFi4 paths in the `webrtc-apm` `sof-hifi` fork:** the one shared place to optimize.
3. **SOF math** for any glue code: FFT with HiFi3 paths that run on HiFi4, STFT, MFCC with a HiFi4 path.
4. **Cadence NDSP** ([ndsplib-hifi4](https://github.com/foss-xtensa/ndsplib-hifi4)) only as a benchmark.

### Out-of-box layers

| Layer | Change |
| --- | --- |
| Firmware config | `app/boards/imx8mp_evk_mimx8ml8_adsp.conf`: webrtc AEC/NS/HPF `=y` (stubs first, then real), heap sized |
| Topology (IPC3, m4) | `webrtc_*.m4` macros, capture pipe m4, volume + demux playback, new imx8mp AEC targets |
| Firmware code | IPC3 switch-control support in the webrtc modules (P5) |
| Kernel | Topology name in the `imx8.c` machine table |
| Userspace | UCM Voice verb in `alsa-ucm-conf` |
| Packaging | `sof-bin` and the NXP Yocto recipe ship the `.ri` and `.tplg` |

### Scaling to future features

- **Keyword detection:** `webrtc_vad` exists, and SOF's IPC3 keyword flow
  (KPB + detector + notification) is documented
  ([KD guide](https://thesofproject.github.io/latest/developer_guides/linux_driver/third_party/keyword_detect/keyword_detect.html)).
  Feed it the cleaned audio.
- **Better NS:** `webrtc_ns2` (RNNoise), only where the chain runs at 48 kHz.
- **Vendor binaries:** built in on IPC3; on IPC4 they load as LLEXT.
- **Always-on listening:** keep VAD/KWD state in OCRAM_A so DDR can go
  low-power ([AN13201](https://www.nxp.com/docs/en/application-note/AN13201.pdf)).

---

## 4. IPC4 variant

Same modules and chain. IPC4 changes three things around them, all mostly reuse:

- **Scheduling:** the chain can run in the DP domain instead of the 1 ms LL tick.
- **Topology:** topology2 can use upstream's `webrtc-*.conf` classes directly.
- **Loading:** the modules load as LLEXT; upstream already builds them `=m` on IPC4.

**Status:** i.MX runs IPC3 today. IPC4 for i.MX is an open PR; once it merges,
the IPC type is picked at load time with the SOF module parameter.

| Aspect | IPC3 (this plan) | IPC4 |
| --- | --- | --- |
| Modules | built in (`=y`) | Same modules as LLEXT (`=m`) |
| Scheduling | LL only; worst-case call < 0.5 ms | AEC/NS in DP, chosen per widget with topology2 `scheduler_domain` |
| Topology | New m4 macros | Reuse `webrtc-aec.conf` and siblings; write only an NXP platform conf |
| AEC inputs | Pipeline-based mic/ref selection | Pin-based: pin 0 mic, pin 1 reference |
| Echo reference | Playback demux -> AEC | Same tap, from copier/gain output pins |
| Vendor binaries | Static link only | Runtime-loaded `.llext` by UUID |
| Kernel | `ipc3-topology.c` | `ipc4-topology.c`, `ipc4-loader.c` |

IPC4 roadmap (after the IPC3 v1):

1. Prove IPC4 on the 8MP EVK on the open PR's branch. Gate: same audio as IPC3, no xruns.
2. LLEXT on i.MX: load the webrtc modules as `.llext` (rimage signing, manifest for imx8m). Biggest unknown.
3. Topology2 voice capture reusing `webrtc-*.conf`; AEC/NS in DP. Gate: ERLE/NR equal to IPC3.
4. Make it the default once IPC4 is the default on i.MX.

Open questions: what the i.MX IPC4 PR covers (DP, copiers on SAI/micfil, LLEXT);
rimage signing for imx8m; KPB under IPC4 on non-Intel platforms.

---

## 5. Glossary

| Term | Meaning |
| --- | --- |
| A53 / M7 | The i.MX8MP's general-purpose cores: 4x Cortex-A53 run Linux, one Cortex-M7 runs an RTOS. Neither is used by this chain. |
| AEC | Acoustic echo cancellation: removes the device's own speaker sound from its microphone, using the playback signal as the reference. |
| AEC3 / AECm / Float | The three engines inside `webrtc_aec`. AEC3: WebRTC's modern C++ canceller, best quality, heaviest, the maintained one. AECm: old fixed-point "mobile" one, cheapest, weakest in double talk. Float: WebRTC's classic floating-point AEC, uses the FPU; our default. |
| AGC | Automatic gain control: keeps the voice level steady. |
| ALSA / PCM | The Linux audio API; a PCM is one playable or recordable stream, e.g. `hw:0,2`. |
| Bulk delay | Fixed time from a sample leaving the DSP for the speaker until its echo reaches the mic. The AEC must cover it. |
| Codec | The chip that turns digital audio into analog for the speaker (wm8960 / wm8962 on the EVK). |
| DAI | Digital audio interface: SOF's endpoint for a hardware audio port (SAI, micfil). |
| Demux | SOF component that copies one stream to several outputs. |
| DNM | "Do not merge": a commit for local use only. |
| Double talk | Both sides talking at once; the hardest AEC case. |
| DSP / HiFi4 / Xtensa | The i.MX8MP's audio processor: a Cadence Tensilica HiFi4 core (Xtensa) at 800 MHz. SOF runs on it. |
| ERLE | Echo return loss enhancement: how many dB the echo is reduced. |
| EVK | Evaluation kit: NXP's i.MX8MP development board. |
| Far-end / near-end | Far-end: the remote party, heard through the speaker. Near-end: the person in the room. |
| FPU / VFPU | Floating-point unit; VFPU is the HiFi4's vector float unit. |
| HPF | High-pass filter: removes rumble and DC before the AEC. |
| IPC3 / IPC4 | The message protocol between Linux and SOF. |
| Kconfig, `=y` / `=m` | Build configuration. `=y` builds a module into the firmware; `=m` builds it as a loadable file (LLEXT). |
| KPB / KWD / VAD | Keyphrase buffer, keyword detector, voice activity detector. |
| LL / DP | SOF scheduling domains: LL runs everything on a 1 ms tick; DP gives heavy modules their own thread. IPC3 has only LL. |
| LLEXT | Zephyr loadable extensions; SOF supports them only with IPC4. |
| MCPS | Million cycles per second, a measure of DSP load. The HiFi4 has 800. |
| micfil (`fsl_micfil`) | The i.MX8MP's PDM microphone interface and its Linux driver. |
| Module / module adapter | A processing block in SOF; the module adapter plugs modules into pipelines. |
| NR / NS | Noise reduction (measured dB) / noise suppression (the module). |
| OCRAM / OCRAM_A / DDR / heap | On-chip RAM (OCRAM_A: 256 KB next to the DSP); external main memory; memory SOF gives modules at runtime. |
| PDM | Pulse-density modulation: the 1-bit format of digital MEMS microphones. |
| Pipeline / period / xrun | A chain of components scheduled together / audio moved per run / a buffer overrun or underrun. |
| Probes | SOF feature that extracts or injects audio at any buffer in a pipeline, for debugging. |
| Raw / Voice PCM | Raw = untouched mic; Voice = after HPF, AEC and NS. |
| RES | Residual echo suppression. |
| rimage / `.ri` | The firmware packaging tool / the firmware file Linux loads into the DSP. |
| RNNoise | A small neural-network noise suppressor (`webrtc_ns2`), 48 kHz. |
| SAI | Synchronous audio interface: the I2S/TDM port to the codec. |
| SDR | Signal-to-distortion ratio. |
| SOF | Sound Open Firmware. |
| SRC | Sample-rate converter. |
| Stub | Placeholder build of a module that passes audio through unchanged. |
| Testbench | SOF tool that runs the same C components on the PC with files. |
| Topology (`.tplg`) | Tells SOF which components and pipelines to create. topology1 = m4 (IPC3), topology2 = `.conf` classes (IPC4). |
| UCM | ALSA use-case configuration. |
| west / west module | Zephyr's repository tool / an extra repo it fetches. |
| XCC / xt-run | Cadence's Xtensa compiler / its cycle-accurate simulator. |
| Zephyr / XTOS | The RTOS SOF runs on / SOF's old OS, now removed. |
