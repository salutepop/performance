#!/usr/bin/env bash
#
# nvmet_lab.sh — 임시 NVMe namespace를 NVMe-oF target(nvmet)으로 생성/정리한다.
#
# 물리 NVMe가 부팅 디스크 하나뿐인 환경에서, 여러 개의 "진짜 NVMe namespace
# 디바이스"(/dev/nvmeXnY)를 만들어 eBPF I/O 트레이서를 멀티-디바이스로 실험하기
# 위한 헬퍼다. 호스트 쪽은 NVMe 드라이버 풀스택을 타므로 nvme_complete_rq가
# 발생하고 D2C 분할(D2CQ/CQ2C)도 정상 동작한다. 백킹 스토어는 .smoke/nvmet/ 의
# 파일(= 실제 nvme0n1 위)이라 데이터는 물리 NVMe에 떨어진다.
#
# transport: loop(네트워크 0) | tcp(루프백) | rdma(RDMA 디바이스 + IP 필요)
#
# 사용법 (root 필요):
#   sudo scripts/nvmet_lab.sh up   [loop|tcp|rdma]
#   sudo scripts/nvmet_lab.sh down
#   sudo scripts/nvmet_lab.sh status
#
# 환경변수 오버라이드:
#   NS_COUNT (기본 4)   NS_SIZE (기본 8G)   ADDR (기본 127.0.0.1)   PORT (기본 4420)
#   BACKING_DIR (기본 <repo>/.smoke/nvmet)   PURGE=1 (down 시 백킹 파일도 삭제)

set -u

CMD="${1:-}"
TRANSPORT="${2:-loop}"
NS_COUNT="${NS_COUNT:-4}"
NS_SIZE="${NS_SIZE:-8G}"
ADDR="${ADDR:-127.0.0.1}"
PORT="${PORT:-4420}"
SUBNQN="nqn.2026-05.cm.perf:nvmet-lab"
PORTID=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKING_DIR="${BACKING_DIR:-$REPO/.smoke/nvmet}"
CFS=/sys/kernel/config/nvmet

log() { echo "[nvmet_lab] $*"; }
die() { echo "[nvmet_lab] ERROR: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "must run as root (sudo)"

# transport별 호스트/타깃 모듈을 로드한다. nvme-loop 한 모듈이 loop의 양쪽
# (호스트 fabric + 타깃 포트)을 모두 등록한다. tcp/rdma는 호스트/타깃 분리.
load_modules() {
    case "$TRANSPORT" in
        loop) modprobe nvmet nvme-loop ;;
        tcp)  modprobe nvmet nvmet-tcp nvme-tcp ;;
        rdma) modprobe nvmet nvmet-rdma nvme-rdma ;;
        *)    die "unknown transport: $TRANSPORT (loop|tcp|rdma)" ;;
    esac || die "modprobe failed for transport=$TRANSPORT"
    [[ -d "$CFS" ]] || die "$CFS missing — nvmet not loaded?"
}

# 이 lab의 subsystem에 연결된 호스트 컨트롤러의 namespace 블록 디바이스를 출력.
show_devices() {
    local found=0 c cn ns
    for c in /sys/class/nvme/nvme*; do
        [[ -r "$c/subsysnqn" ]] || continue
        [[ "$(cat "$c/subsysnqn" 2>/dev/null)" == "$SUBNQN" ]] || continue
        cn="$(basename "$c")"
        for ns in /dev/${cn}n*; do
            [[ -b "$ns" ]] || continue
            log "  device: $ns"
            found=$((found + 1))
        done
    done
    if [[ "$found" -gt 0 ]]; then
        log "$found NVMe namespace device(s) ready"
    else
        log "no connected devices for $SUBNQN"
    fi
}

do_up() {
    [[ -e "$CFS/subsystems/$SUBNQN" ]] && die "already set up — run 'down' first"
    load_modules
    mkdir -p "$BACKING_DIR"

    log "creating subsystem $SUBNQN ($NS_COUNT namespace(s), $NS_SIZE each)"
    mkdir -p "$CFS/subsystems/$SUBNQN"
    echo 1 > "$CFS/subsystems/$SUBNQN/attr_allow_any_host"

    local i img nsd
    for ((i = 1; i <= NS_COUNT; i++)); do
        img="$BACKING_DIR/ns$i.img"
        if [[ -f "$img" ]]; then
            log "  ns$i backing $img (reuse)"
        else
            truncate -s "$NS_SIZE" "$img"
            log "  ns$i backing $img ($NS_SIZE, sparse)"
        fi
        nsd="$CFS/subsystems/$SUBNQN/namespaces/$i"
        mkdir -p "$nsd"
        printf '%s' "$img" > "$nsd/device_path"
        echo 1 > "$nsd/enable"
    done

    log "creating port $PORTID (trtype=$TRANSPORT)"
    mkdir -p "$CFS/ports/$PORTID"
    echo "$TRANSPORT" > "$CFS/ports/$PORTID/addr_trtype"
    if [[ "$TRANSPORT" != loop ]]; then
        echo ipv4    > "$CFS/ports/$PORTID/addr_adrfam"
        echo "$ADDR" > "$CFS/ports/$PORTID/addr_traddr"
        echo "$PORT" > "$CFS/ports/$PORTID/addr_trsvcid"
    fi
    ln -s "$CFS/subsystems/$SUBNQN" "$CFS/ports/$PORTID/subsystems/$SUBNQN"

    log "connecting host (transport=$TRANSPORT)"
    case "$TRANSPORT" in
        loop) nvme connect -t loop -n "$SUBNQN" ;;
        tcp)  nvme connect -t tcp  -a "$ADDR" -s "$PORT" -n "$SUBNQN" ;;
        rdma) nvme connect -t rdma -a "$ADDR" -s "$PORT" -n "$SUBNQN" ;;
    esac || die "nvme connect failed (transport=$TRANSPORT)"

    udevadm settle 2>/dev/null || sleep 1
    show_devices
    log "done. trace e.g.:  python3 monitoring/collectors/ebpf_io/collector.py -m libaio ..."
}

do_down() {
    log "disconnecting host controllers for $SUBNQN"
    nvme disconnect -n "$SUBNQN" >/dev/null 2>&1 || true

    [[ -L "$CFS/ports/$PORTID/subsystems/$SUBNQN" ]] && \
        rm -f "$CFS/ports/$PORTID/subsystems/$SUBNQN"
    [[ -d "$CFS/ports/$PORTID" ]] && rmdir "$CFS/ports/$PORTID" 2>/dev/null || true

    if [[ -d "$CFS/subsystems/$SUBNQN/namespaces" ]]; then
        local d
        for d in "$CFS/subsystems/$SUBNQN/namespaces/"*/; do
            [[ -d "$d" ]] || continue
            echo 0 > "$d/enable" 2>/dev/null || true
            rmdir "$d" 2>/dev/null || true
        done
    fi
    [[ -d "$CFS/subsystems/$SUBNQN" ]] && rmdir "$CFS/subsystems/$SUBNQN" 2>/dev/null || true
    log "configfs torn down (modules left loaded)"

    if [[ "${PURGE:-0}" == "1" ]]; then
        rm -rf "$BACKING_DIR"
        log "purged backing files ($BACKING_DIR)"
    else
        log "backing files kept ($BACKING_DIR) — re-run with PURGE=1 down to delete"
    fi
}

do_status() {
    if [[ -d "$CFS/subsystems/$SUBNQN" ]]; then
        log "target: $SUBNQN present"
        local n
        for n in "$CFS/subsystems/$SUBNQN/namespaces/"*/; do
            [[ -d "$n" ]] || continue
            log "  ns $(basename "$n") -> $(cat "$n/device_path" 2>/dev/null)"
        done
    else
        log "target: not set up"
    fi
    show_devices
}

case "$CMD" in
    up)     do_up ;;
    down)   do_down ;;
    status) do_status ;;
    *) echo "usage: sudo $0 {up [loop|tcp|rdma] | down | status}" >&2; exit 1 ;;
esac
