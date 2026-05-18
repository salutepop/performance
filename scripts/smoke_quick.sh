#!/bin/bash
# 자율 개발 루프의 회귀 검증 baseline.
# 종료 코드 0 = 통과. 0이 아니면 마지막 변경 커밋 금지.
# 검증 항목: BPF build OK, io_profiler 6s smoke 통과, 산출물 파일 무결성.
set -e
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EBPF="$ROOT/ebpf"
TMP_FIO="/tmp/fio_smoke.dat"
RUNTIME=${RUNTIME:-6}

cd "$EBPF"

echo "[smoke] 1/4 build io_trace"
rm -f io_trace.bpf.o io_trace.skel.h io_trace
make >/dev/null 2>&1
[ -x ./io_trace ] || { echo "[smoke] build failed"; exit 1; }

echo "[smoke] 2/4 run io_profiler (libaio, ${RUNTIME}s)"
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

timeout 40 python3 io_profiler.py -m libaio -i 1 \
    -c "fio --name=smoke --filename=$TMP_FIO --rw=randrw --rwmixread=50 \
        --bs=4k --iodepth=32 --size=256M --runtime=$RUNTIME --time_based \
        --direct=1 --ioengine=libaio --group_reporting --numjobs=2" \
    >"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    echo "[smoke] io_profiler exit=$RC"
    tail -30 "$LOG"
    exit 1
fi

echo "[smoke] 3/4 verify artifacts"
LATEST_DEV=$(ls -t csv_results/nvme*_*.csv 2>/dev/null | head -1)
LATEST_SYS=$(ls -t csv_results/system_metrics_*.csv 2>/dev/null | head -1)
LATEST_TOPO=$(ls -t csv_results/topology_*.json 2>/dev/null | head -1)

[ -s "$LATEST_DEV"  ] || { echo "[smoke] device csv missing"; exit 1; }
[ -s "$LATEST_SYS"  ] || { echo "[smoke] system csv missing"; exit 1; }
[ -s "$LATEST_TOPO" ] || { echo "[smoke] topology json missing"; exit 1; }

DEV_ROWS=$(wc -l <"$LATEST_DEV")
SYS_ROWS=$(wc -l <"$LATEST_SYS")
[ "$DEV_ROWS" -gt 2 ] || { echo "[smoke] device csv has $DEV_ROWS lines"; exit 1; }
[ "$SYS_ROWS" -gt 2 ] || { echo "[smoke] system csv has $SYS_ROWS lines"; exit 1; }

echo "[smoke] 4/4 sanity (QD column)"
# 픽스된 QD가 음수/억대로 안 가야 함 (per earlier bug)
if grep -E "Curr=-[0-9]+|Max=[0-9]{5,}" "$LOG"; then
    echo "[smoke] QD insane values detected in final summary"
    exit 1
fi

echo "[smoke] PASS  device_rows=$DEV_ROWS sys_rows=$SYS_ROWS"
exit 0
