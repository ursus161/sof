#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
#
# voice-loop.sh - build -> deploy -> measure loop for the i.MX8MP voice
# chain (webrtc_aec / webrtc_ns on the HiFi4, IPC3).
#
# Usage: voice-loop.sh [build|deploy|measure|all]   (default: all)
# Config: scripts/voice-loop/voice-loop.env (copy voice-loop.env.example).
# Exit status: 0 = PASS, non-zero = build/deploy error or FAIL gate.
# Results: $OUT_DIR/<timestamp>/ (WAVs, logs, metrics.json) and
#          $OUT_DIR/latest -> that directory.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOF_TOP=$(cd "$HERE/../.." && pwd)
ENV_FILE=${VOICE_LOOP_ENV:-$HERE/voice-loop.env}
# shellcheck source=/dev/null
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

: "${EVK_HOST:?set EVK_HOST (ssh target, e.g. root@imx8mp-evk) in $ENV_FILE}"
: "${WEST_TOP:=$(cd "$SOF_TOP/.." && pwd)}"
: "${BUILD_CMD:=$SOF_TOP/scripts/xtensa-build-zephyr.py imx8m}"
: "${FW_IMAGE:=$WEST_TOP/build-sof-staging/sof/imx/sof/sof-imx8m.ri}"
: "${TPLG_NAME:=sof-imx8mp-wm8960-aec}"
: "${TPLG_BUILD_DIR:=$HERE/out/build-tplg}"
: "${EVK_FW_DIR:=/lib/firmware/imx/sof}"
: "${EVK_TPLG_DIR:=/lib/firmware/imx/sof-tplg}"
: "${RELOAD_CMD:=modprobe -r snd_sof_imx8 && modprobe snd_sof_imx8}"
: "${PLAY_PCM:=hw:0,0}"
: "${RAW_PCM:=hw:0,1}"
: "${VOICE_PCM:=hw:0,2}"
: "${RAW_FMT:=-f S16_LE -r 48000 -c 1}"
: "${VOICE_FMT:=-f S16_LE -r 16000 -c 1}"
: "${GATES:=nr,xrun}"
: "${FAR_END_WAV:=}"
: "${OUT_DIR:=$HERE/out}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$EVK_HOST")
log() { printf '[voice-loop] %s\n' "$*" >&2; }

stage_build() {
	log "firmware: $BUILD_CMD"
	(cd "$WEST_TOP" && eval "$BUILD_CMD")
	[ -f "$FW_IMAGE" ] || { log "missing $FW_IMAGE (set FW_IMAGE)"; return 2; }

	log "topology: $TPLG_NAME"
	cmake -S "$SOF_TOP/tools/topology" -B "$TPLG_BUILD_DIR" -G Ninja >/dev/null
	ninja -C "$TPLG_BUILD_DIR" "topology1_$TPLG_NAME"
	TPLG_FILE=$(find "$TPLG_BUILD_DIR" -name "$TPLG_NAME.tplg" -print -quit)
	[ -n "$TPLG_FILE" ] || { log "topology $TPLG_NAME.tplg not built"; return 2; }
	echo "$TPLG_FILE" > "$TPLG_BUILD_DIR/.voice-loop-tplg"
}

stage_deploy() {
	TPLG_FILE=$(cat "$TPLG_BUILD_DIR/.voice-loop-tplg")
	log "deploy to $EVK_HOST"
	scp -q "$FW_IMAGE" "$EVK_HOST:$EVK_FW_DIR/sof-imx8m.ri"
	scp -q "$TPLG_FILE" "$EVK_HOST:$EVK_TPLG_DIR/$TPLG_NAME.tplg"
	"${SSH[@]}" "dmesg -C; $RELOAD_CMD"
	"${SSH[@]}" "for i in \$(seq 20); do aplay -l | grep -q card && exit 0; sleep 0.5; done; exit 1" \
		|| { log "sound card did not come back"; "${SSH[@]}" dmesg | tail -40 >&2; return 3; }
}

stage_measure() {
	local run
	run="$OUT_DIR/$(date +%Y%m%d-%H%M%S)"
	mkdir -p "$run"
	ln -sfn "$run" "$OUT_DIR/latest"

	python3 "$HERE/gen_stimulus.py" "$run/stimulus.wav" ${FAR_END_WAV:+--far-end "$FAR_END_WAV"}
	local dur
	dur=$(python3 -c "import json;print(int(json.load(open('$run/stimulus.wav.json'))['end'])+1)")

	scp -q "$run/stimulus.wav" "$EVK_HOST:/tmp/vl-stim.wav"
	log "measure: play $PLAY_PCM, record raw $RAW_PCM + voice $VOICE_PCM for ${dur}s"
	# All three start in one shell so they share a time origin (within a few ms).
	"${SSH[@]}" "cd /tmp && rm -f vl-*.log;
		arecord -D $RAW_PCM $RAW_FMT -d $dur vl-raw.wav 2> vl-arec-raw.log &
		arecord -D $VOICE_PCM $VOICE_FMT -d $dur vl-voice.wav 2> vl-arec-voice.log &
		aplay -D $PLAY_PCM vl-stim.wav 2> vl-aplay.log;
		wait; dmesg > vl-dmesg.log" || true
	for f in vl-raw.wav vl-voice.wav vl-arec-raw.log vl-arec-voice.log vl-aplay.log vl-dmesg.log; do
		scp -q "$EVK_HOST:/tmp/$f" "$run/${f#vl-}" || log "missing $f on target"
	done

	python3 "$HERE/voice_metrics.py" "$run/raw.wav" "$run/voice.wav" \
		"$run/stimulus.wav.json" --gates "$GATES" \
		--logs "$run/arec-raw.log" "$run/arec-voice.log" "$run/aplay.log" "$run/dmesg.log" \
		| tee "$run/metrics.txt"
	local rc=${PIPESTATUS[0]}
	tail -n1 "$run/metrics.txt" > "$run/metrics.json"
	return "$rc"
}

case "${1:-all}" in
build) stage_build ;;
deploy) stage_deploy ;;
measure) stage_measure ;;
all) stage_build && stage_deploy && stage_measure ;;
*) echo "usage: $0 [build|deploy|measure|all]" >&2; exit 64 ;;
esac
