#!/bin/bash
# Post-switch verification (APPLY.md step 3), safe to run repeatedly.
cd ${FRIGATE_HOME:-$HOME/frigate}
echo "== API"; curl -s http://127.0.0.1:5000/api/version; echo
echo "== cameras"; curl -s 127.0.0.1:5000/api/stats | python3 -c "
import json,sys; d=json.load(sys.stdin)
for k,v in d['cameras'].items(): print(f\"{k:14s} cam {v['camera_fps']:5} det {v['detection_fps']:5} skip {v['skipped_fps']}\")
print('detector ms', d['detectors']['host_zmq']['inference_speed'])"
echo "== rewritten ffmpeg commands (expect one rewrite=1 per live camera)"; docker exec frigate cat /dev/shm/logs/frigate/current | grep -c 'rewrite=1'
echo "== ffmpeg processes"; docker exec frigate ps -eo pid,pcpu,rss,args | grep '[o]pt/ffmpeg-vk/bin/ffmpeg' | awk '{print $1, $2"%", $3"KB"}'
echo "== process names (must be ffmpeg, else the maintainer deletes live segments)"; docker exec frigate bash -c 'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "$c" in *8554/*rawvideo*) cat $p/comm;; esac; done | sort | uniq -c'
echo "== decoder holders"; sudo fuser -v /dev/video0 2>&1 | tail -n +2
echo "== Honeykrisp in use"; docker exec frigate sh -c 'grep -l libvulkan_asahi /proc/[0-9]*/maps 2>/dev/null | wc -l'
echo "== errors (excluding offline cam1)"; docker exec frigate cat /dev/shm/logs/frigate/current | grep -E 'ERROR|Restarting ffmpeg' | grep -v cam1 | tail -5
echo "== kernel"; sudo dmesg | grep -iE 'avd|dart' | tail -3
echo "== container CPU (baseline was ~107%)"; docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}' frigate
echo "== recordings in last 2 min"; find recordings -mmin -2 -name '*.mp4' | wc -l
echo "== detector journal"; journalctl -u frigate-detector --since -10min --no-pager | grep -ciE 'fallback|error'
