#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
#
# voice-loop.sh - closed loop for the i.MX8MP voice chain
# (micfil -> webrtc_hpf -> webrtc_aec -> SRC 48k->16k -> webrtc_ns, IPC3).
#
#   build    firmware (.ri) + topology (.tplg)
#   bench    deterministic: SOF host testbench on synthetic/recorded WAVs,
#            exact near-end and double-talk scores (no board needed)
#   deploy   copy .ri/.tplg to the EVK and reload SOF
#   measure  acoustic: EVK plays far-end, a second device plays near-end,
#            Raw + Voice PCMs recorded at once, then scored
#   all      build -> bench -> deploy -> measure, stops at the first FAIL
#
# Config: scripts/voice-loop/voice-loop.env (from voice-loop.env.example).
# Results: $OUT_DIR/<stage>-<timestamp>/ and $OUT_DIR/latest.
# Exit status 0 = PASS (or stage done), non-zero = error or FAIL.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOF_TOP=$(cd "$HERE/../.." && pwd)
ENV_FILE=${VOICE_LOOP_ENV:-$HERE/voice-loop.env}
# shellcheck source=/dev/null
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

: "${WEST_TOP:=$(cd "$SOF_TOP/.." && pwd)}"
: "${BUILD_CMD:=$SOF_TOP/scripts/xtensa-build-zephyr.py imx8m}"
: "${FW_IMAGE:=$WEST_TOP/build-sof-staging/sof/imx/sof/sof-imx8m.ri}"
: "${TPLG_NAME:=sof-imx8mp-wm8960-aec}"
: "${OUT_DIR:=$HERE/out}"
: "${TPLG_BUILD_DIR:=$OUT_DIR/build-tplg}"
: "${STIM_ARGS:=}"
# bench
: "${TESTBENCH:=$SOF_TOP/tools/testbench/build_testbench/install/bin/sof-testbench4}"
: "${BENCH_TPLG:=}"
: "${BENCH_PIPES:=1,2}"
: "${BENCH_ARGS:=-r 48000 -c 1 -b S16_LE -R 16000 -n 1}"
: "${BENCH_GATES:=erle,nr,near,dt,dtsdr}"
# evk
: "${EVK_HOST:=}"
: "${EVK_FW_DIR:=/lib/firmware/imx/sof}"
: "${EVK_TPLG_DIR:=/lib/firmware/imx/sof-tplg}"
: "${RELOAD_CMD:=modprobe -r snd_sof_imx8 && modprobe snd_sof_imx8}"
: "${PLAY_PCM:=hw:0,0}"
: "${RAW_PCM:=hw:0,1}"
: "${VOICE_PCM:=hw:0,2}"
: "${RAW_FMT:=-f S16_LE -r 48000 -c 1}"
: "${VOICE_FMT:=-f S16_LE -r 16000 -c 1}"
: "${NEAR_PLAY_CMD:=}"
: "${GATES:=passlvl,passcorr,xrun}"

log() { printf '[voice-loop] %s\n' "$*" >&2; }
need_evk() { [ -n "$EVK_HOST" ] || { log "set EVK_HOST in $ENV_FILE"; exit 2; }; }
SSH() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$EVK_HOST" "$@"; }

new_run() {
	RUN="$OUT_DIR/$1-$(date +%Y%m%d-%H%M%S)"
	mkdir -p "$RUN"
	ln -sfn "$RUN" "$OUT_DIR/latest"
	# shellcheck disable=SC2086
	python3 "$HERE/gen_stimulus.py" "$RUN" $STIM_ARGS
}

stage_build() {
	log "firmware: $BUILD_CMD"
	(cd "$WEST_TOP" && eval "$BUILD_CMD")
	[ -f "$FW_IMAGE" ] || { log "missing $FW_IMAGE (set FW_IMAGE)"; return 2; }
	log "topology: $TPLG_NAME"
	cmake -S "$SOF_TOP/tools/topology" -B "$TPLG_BUILD_DIR" -G Ninja >/dev/null
	ninja -C "$TPLG_BUILD_DIR" "topology1_$TPLG_NAME"
	local f
	f=$(find "$TPLG_BUILD_DIR" -name "$TPLG_NAME.tplg" -print -quit)
	[ -n "$f" ] || { log "topology $TPLG_NAME.tplg not built"; return 2; }
	echo "$f" > "$OUT_DIR/.tplg"
}

stage_bench() {
	if [ -z "$BENCH_TPLG" ]; then
		log "bench: SKIP (set BENCH_TPLG; webrtc modules must first build in the host library, P1)"
		return 0
	fi
	[ -x "$TESTBENCH" ] || { log "no testbench at $TESTBENCH (scripts/rebuild-testbench.sh)"; return 2; }
	new_run bench
	log "bench: $TESTBENCH pipes $BENCH_PIPES"
	# Input order follows BENCH_PIPES: playback (reference) first, capture (mic) second.
	# shellcheck disable=SC2086
	"$TESTBENCH" -t "$BENCH_TPLG" -p "$BENCH_PIPES" $BENCH_ARGS \
		-i "$RUN/far.raw,$RUN/mic_bench.raw" \
		-o "$RUN/bench_play.raw,$RUN/voice.raw" > "$RUN/testbench.log" 2>&1 \
		|| { log "testbench failed, see $RUN/testbench.log"; return 3; }
	python3 "$HERE/voice_metrics.py" bench "$RUN/mic_bench.wav" "$RUN/voice.raw" \
		"$RUN/layout.json" --near-clean "$RUN/near_clean.wav" --gates "$BENCH_GATES" \
		| tee "$RUN/metrics.txt"
	local rc=${PIPESTATUS[0]}
	tail -n1 "$RUN/metrics.txt" > "$RUN/metrics.json"
	return "$rc"
}

stage_deploy() {
	need_evk
	local f
	f=$(cat "$OUT_DIR/.tplg")
	log "deploy to $EVK_HOST"
	scp -q "$FW_IMAGE" "$EVK_HOST:$EVK_FW_DIR/sof-imx8m.ri"
	scp -q "$f" "$EVK_HOST:$EVK_TPLG_DIR/$TPLG_NAME.tplg"
	SSH "dmesg -C; $RELOAD_CMD"
	SSH "for i in \$(seq 20); do aplay -l | grep -q card && exit 0; sleep 0.5; done; exit 1" \
		|| { log "sound card did not come back"; SSH dmesg | tail -40 >&2; return 3; }
}

stage_measure() {
	need_evk
	new_run evk
	local dur gates=$GATES
	dur=$(python3 -c "import json;print(int(json.load(open('$RUN/layout.json'))['end'])+1)")
	if [ -z "$NEAR_PLAY_CMD" ]; then
		log "measure: no NEAR_PLAY_CMD, near-end gate skipped (bench covers it)"
		gates=$(echo ",$gates," | sed 's/,near,/,/g; s/^,//; s/,$//')
	fi
	scp -q "$RUN/far.wav" "$EVK_HOST:/tmp/vl-far.wav"
	log "measure: ${dur}s, play $PLAY_PCM, record raw $RAW_PCM + voice $VOICE_PCM"
	# Recording and far-end playback start in one remote shell (shared time origin);
	# the near-end talker starts locally at about the same moment, the ~0.1-0.3 s
	# skew is covered by the window margins.
	SSH "cd /tmp && rm -f vl-*.log;
		arecord -D $RAW_PCM $RAW_FMT -d $dur vl-raw.wav 2> vl-arec-raw.log &
		arecord -D $VOICE_PCM $VOICE_FMT -d $dur vl-voice.wav 2> vl-arec-voice.log &
		aplay -D $PLAY_PCM vl-far.wav 2> vl-aplay.log;
		wait; dmesg > vl-dmesg.log" &
	local remote=$!
	if [ -n "$NEAR_PLAY_CMD" ]; then
		(sleep 0.3; eval "${NEAR_PLAY_CMD//\{wav\}/$RUN/near.wav}") &
	fi
	wait "$remote" || true
	wait || true
	for f in vl-raw.wav vl-voice.wav vl-arec-raw.log vl-arec-voice.log vl-aplay.log vl-dmesg.log; do
		scp -q "$EVK_HOST:/tmp/$f" "$RUN/${f#vl-}" || log "missing $f on target"
	done
	python3 "$HERE/voice_metrics.py" evk "$RUN/raw.wav" "$RUN/voice.wav" "$RUN/layout.json" \
		--gates "$gates" \
		--logs "$RUN/arec-raw.log" "$RUN/arec-voice.log" "$RUN/aplay.log" "$RUN/dmesg.log" \
		| tee "$RUN/metrics.txt"
	local rc=${PIPESTATUS[0]}
	tail -n1 "$RUN/metrics.txt" > "$RUN/metrics.json"
	return "$rc"
}

mkdir -p "$OUT_DIR"
case "${1:-all}" in
build) stage_build ;;
bench) stage_bench ;;
deploy) stage_deploy ;;
measure) stage_measure ;;
all) stage_build && stage_bench && stage_deploy && stage_measure ;;
*) echo "usage: $0 [build|bench|deploy|measure|all]" >&2; exit 64 ;;
esac
