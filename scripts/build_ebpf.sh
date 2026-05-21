#!/usr/bin/env bash
#
# build_ebpf.sh — eBPF I/O 트레이서(io_trace) 빌드 헬퍼. ARM(aarch64)/x86_64 공용.
#
# vmlinux.h 와 io_trace.skel.h 는 git에 추적되지 않는다 — 커널 BTF·아키텍처
# 종속 생성물이라 머신마다 달라지기 때문이다. 이 스크립트는 빌드 때마다 현재
# 커널(/sys/kernel/btf/vmlinux)에서 vmlinux.h 를 새로 뽑은 뒤 전체를 다시 빌드한다.
# 그래서 ARM↔x86 머신을 오가도 별도 조작 없이 그냥 다시 실행하면 된다.
#
# 사용법:
#   scripts/build_ebpf.sh            의존성 점검 후 클린 빌드 (기본)
#   scripts/build_ebpf.sh --check    의존성/환경만 점검 (빌드 안 함)
#   scripts/build_ebpf.sh --clean    생성물 전부 삭제 (binary·obj·skeleton·vmlinux.h)
#   scripts/build_ebpf.sh --deps     빌드 의존성을 apt로 설치(root 필요) 후 빌드
#   scripts/build_ebpf.sh --help     이 도움말
#
# 빌드 의존성: clang, make, gcc, bpftool, libbpf-dev, libelf-dev, zlib1g-dev.
# 커널은 BTF가 켜져 있어야 한다 (/sys/kernel/btf/vmlinux 존재).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/monitoring/collectors/ebpf_io/src"
APT_PKGS="build-essential clang bpftool libbpf-dev libelf-dev zlib1g-dev"

log()  { echo "[build_ebpf] $*"; }
warn() { echo "[build_ebpf] WARN: $*" >&2; }
die()  { echo "[build_ebpf] ERROR: $*" >&2; exit 1; }

usage() {
    sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^#\s\?//'
}

# uname -m → Makefile/BPF ARCH (Makefile의 매핑과 동일).
detect_arch() {
    case "$(uname -m)" in
        x86_64)        echo "x86" ;;
        aarch64|arm64) echo "arm64" ;;
        *)             die "unsupported arch: $(uname -m) (x86_64/aarch64만 지원)" ;;
    esac
}

# 필수 커맨드 + dev 헤더 존재를 점검. 누락 항목을 전역 MISSING 에 채운다.
MISSING=""
check_deps() {
    MISSING=""
    local cmd
    for cmd in clang make gcc bpftool; do
        command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"
    done
    [[ -e /usr/include/bpf/bpf_helpers.h ]] || MISSING="$MISSING libbpf-dev"
    { [[ -e /usr/include/libelf.h ]] || [[ -e /usr/include/gelf.h ]]; } \
        || MISSING="$MISSING libelf-dev"
    [[ -e /usr/include/zlib.h ]] || MISSING="$MISSING zlib1g-dev"
    MISSING="${MISSING# }"
}

install_deps() {
    command -v apt-get >/dev/null 2>&1 \
        || die "apt-get 없음 — 수동 설치 필요: $APT_PKGS"
    [[ "$(id -u)" -eq 0 ]] || die "--deps 는 root 권한 필요 (sudo로 실행)"
    log "installing build deps via apt: $APT_PKGS"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $APT_PKGS
}

# 환경 + 의존성 점검. 통과 시 0, 의존성 누락 시 1.
do_check() {
    local arch; arch="$(detect_arch)"
    log "arch: $(uname -m) → ARCH=$arch"
    [[ -d "$SRC" ]] || die "src 디렉터리 없음: $SRC"
    [[ -r /sys/kernel/btf/vmlinux ]] \
        || die "/sys/kernel/btf/vmlinux 없음 — 커널 BTF(CONFIG_DEBUG_INFO_BTF)가 필요하다"
    log "kernel BTF: /sys/kernel/btf/vmlinux OK"
    check_deps
    if [[ -n "$MISSING" ]]; then
        warn "missing build deps: $MISSING"
        warn "설치:  sudo $0 --deps   (또는  sudo apt-get install $APT_PKGS )"
        return 1
    fi
    log "all build deps present"
    return 0
}

do_clean() {
    log "removing generated files (io_trace, *.bpf.o, skeleton, vmlinux.h)"
    rm -f "$SRC/io_trace" "$SRC/io_trace.bpf.o" "$SRC/io_trace.skel.h" "$SRC/vmlinux.h"
}

do_build() {
    local arch; arch="$(detect_arch)"
    # vmlinux.h 를 포함해 전부 새로 — 아키텍처/커널이 바뀌어도 안전하다.
    do_clean
    log "building io_trace (ARCH=$arch) ..."
    make -C "$SRC"
    [[ -x "$SRC/io_trace" ]] || die "build 끝났는데 io_trace 바이너리가 없다"
    log "build OK → $SRC/io_trace"
    file "$SRC/io_trace" | sed 's/^/[build_ebpf]   /'
}

main() {
    case "${1:-}" in
        --help|-h) usage; exit 0 ;;
        --check)   do_check && exit 0 || exit 1 ;;
        --clean)   do_clean; log "clean done"; exit 0 ;;
        --deps)
            install_deps
            do_check || die "deps 설치 후에도 점검 실패 — 위 안내 확인"
            do_build ;;
        "")
            do_check || die "의존성 누락 — --deps 로 자동 설치하거나 위 안내대로 설치"
            do_build ;;
        *) die "unknown option: $1  (--check | --clean | --deps | --help)" ;;
    esac
}

main "$@"
