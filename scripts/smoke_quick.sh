#!/bin/bash
# 자율 개발 루프의 회귀 검증 baseline.
# 종료 코드 0 = 통과. 0이 아니면 마지막 변경 커밋 금지.
# 검증 항목:
#   1) BPF 빌드 OK
#   2) io_profiler libaio smoke 통과 (fio + sysmon thread)
#   3) 산출물 무결성 (device CSV / system CSV / topology JSON)
#   4) topology JSON 스키마 (nodes/nvme_controllers/cpu_to_node 키 존재)
#   5) GPU가 토폴로지에 있으면 system_metrics CSV에도 gpu*_pwr_w 컬럼 있어야 함
#   6) QD 값 sanity (음수/억대 거부)
#   7) report (HTML + MD) 생성이 깨지지 않음
set -e
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EBPF="$ROOT/ebpf"
TMP_FIO="/tmp/fio_smoke.dat"
RUNTIME=${RUNTIME:-6}

cd "$EBPF"

echo "[smoke] 1/7 build io_trace"
rm -f io_trace.bpf.o io_trace.skel.h io_trace
make >/dev/null 2>&1
[ -x ./io_trace ] || { echo "[smoke] build failed"; exit 1; }

echo "[smoke] 2/7 run io_profiler (libaio, ${RUNTIME}s)"
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

echo "[smoke] 3/7 verify artifacts exist"
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

echo "[smoke] 4/7 topology schema"
python3 - <<PY
import json, sys
with open("$LATEST_TOPO") as f:
    t = json.load(f)
required = {"session_id", "nodes", "cpu_to_node", "nvme_controllers", "gpus"}
missing = required - set(t)
if missing:
    print(f"[smoke] topology missing keys: {missing}", file=sys.stderr)
    sys.exit(1)
if not t["nodes"]:
    print("[smoke] topology has no NUMA nodes", file=sys.stderr); sys.exit(1)
if not t["nvme_controllers"]:
    print("[smoke] topology has no NVMe controllers (worth verifying on this host)", file=sys.stderr)
print(f"[smoke] topology OK: {len(t['nodes'])} node(s), {len(t['nvme_controllers'])} nvme, {len(t['gpus'])} gpu")
PY

echo "[smoke] 5/7 conditional GPU column check"
HAS_GPU=$(python3 -c "import json; print('1' if json.load(open('$LATEST_TOPO')).get('gpus') else '0')")
if [ "$HAS_GPU" = "1" ]; then
    if ! head -1 "$LATEST_SYS" | grep -qE "gpu[0-9]+_pwr_w"; then
        echo "[smoke] topology says GPU exists but system_metrics CSV has no gpu*_pwr_w column"
        exit 1
    fi
    echo "[smoke] GPU column present in system CSV"
else
    echo "[smoke] no GPU in topology — skipping GPU column check"
fi

echo "[smoke] 6/7 QD sanity"
if grep -E "Curr=-[0-9]+|Max=[0-9]{5,}" "$LOG"; then
    echo "[smoke] QD insane values detected"
    exit 1
fi

echo "[smoke] 7/7 report generation"
cd "$ROOT"
python3 -m report.html_report --session-dir "$EBPF/csv_results" >/dev/null
python3 -m report.md_report   --session-dir "$EBPF/csv_results" >/dev/null
SID=$(basename "$LATEST_TOPO" .json | sed 's/^topology_//')
[ -s "$EBPF/csv_results/report_${SID}.html" ] || { echo "[smoke] html report missing"; exit 1; }
[ -s "$EBPF/csv_results/report_${SID}.md"   ] || { echo "[smoke] md report missing"; exit 1; }
# HTML 안에 canvas + Chart.js script 가 있어야 함
grep -q "<canvas" "$EBPF/csv_results/report_${SID}.html"   || { echo "[smoke] html report missing canvas"; exit 1; }
grep -q "cdn.jsdelivr.net/npm/chart.js" "$EBPF/csv_results/report_${SID}.html" || { echo "[smoke] html report missing chart.js script"; exit 1; }

echo "[smoke] PASS  device_rows=$DEV_ROWS sys_rows=$SYS_ROWS sid=$SID has_gpu=$HAS_GPU"
exit 0
