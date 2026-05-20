# uv 사용 가이드

이 프로젝트는 [`uv`](https://docs.astral.sh/uv/)로 Python 환경 관리. 현재 외부 의존성은 없지만 (stdlib만 사용), 향후 패키지 추가 시 lock + venv 관리를 위해 셋업.

`pyproject.toml` 에 정의된 사양: name=`pmon`, requires-python `>=3.12`.

## 1. 처음 설정 (clone 직후)

```bash
cd /home/cm/src/cm/performance

# uv가 .venv 생성 + python 3.12 다운로드 + lock 적용 (deps 0이라 빈 venv)
uv sync
```

확인:
```bash
uv tree
# 예상 출력:
# Resolved 1 package in <Ns>
# pmon v0.1.0
```

`.venv/` 가 프로젝트 루트에 생기고, uv가 관리하는 Python 3.12.x 가 그 안에 들어감.

## 2. 일상 실행 (가장 자주 쓰는 패턴)

### (A) `uv run` 으로 호출 — 권장

`uv run <명령>` 은 자동으로 `.venv` 활성화 + 의존성 sync 보장 후 명령 실행.

```bash
# 측정 + 자동 리포트
uv run ./pmon.py monitor --fio "fio --name=t --filename=/tmp/x \
    --rw=randread --bs=4k --iodepth=32 --size=512M --runtime=30 \
    --time_based --direct=1 --ioengine=libaio --numjobs=4 \
    --group_reporting"

# 가장 최근 세션에서 리포트만 재생성
uv run ./pmon.py report

# 두 세션 비교
uv run ./pmon.py diff --baseline 20260519_001707 --candidate 20260519_002145

# JSON 요약 export
uv run ./pmon.py summary

# 모듈 직접 호출
uv run python -m report
uv run python -m report.diff --baseline 20260519_001707 --candidate 20260519_002145
uv run python -m workloads.scenarios.sample_randread
uv run python -m workloads.scenarios.pcie_contention
uv run python -m workloads.scenarios.gc_stress
```

### (B) `python3` 직접 호출 — 여전히 호환

deps 가 0이라서 system `python3` 도 그대로 동작:

```bash
python3 ./pmon.py monitor --fio "..."
python3 -m report
python3 -m workloads.scenarios.sample_randread
```

차이:
- `uv run` → `.venv/bin/python` (uv 관리 Python 3.12.13)
- `python3` → 시스템 `/usr/bin/python3` (3.12.3)

stdlib 만 쓰니까 둘 다 결과 동일. 다른 사람이 clone 했을 때 환경 차이를 막으려면 `uv run` 추천.

## 3. smoke 검증

`pmon.py debug` 가 개발 검증용 self-test다. 종료 코드 0 = 통과:

```bash
./pmon.py debug                  # 4-phase fio + eBPF + 전체 리포트 E2E
./pmon.py debug --duration 1     # 워크로드 페이즈당 1초로 단축 (기본 3초)
```

uv venv에서 돌리려면:
```bash
uv run ./pmon.py debug
```

## 4. 의존성 추가 (필요해질 때)

```bash
# 예: matplotlib 추가
uv add matplotlib

# 개발용 의존성 (테스트, 린팅 등)
uv add --dev pytest ruff mypy

# 특정 버전 고정
uv add "pandas>=2.0,<3.0"

# 제거
uv remove matplotlib
```

`uv add` 는 자동으로:
- `pyproject.toml` 의 `dependencies` 또는 `[tool.uv] dev-dependencies` 업데이트
- `uv.lock` 갱신
- `.venv` 에 설치

추가 후 다른 환경에서 동기화:
```bash
uv sync
```

## 5. Python 버전 관리

`.python-version` 에 `3.12` 명시. 다른 minor 버전으로 옮기려면:

```bash
# 사용 가능한 버전 보기
uv python list

# 변경
uv python pin 3.13

# 적용 (.venv 재생성)
rm -rf .venv && uv sync
```

## 6. 스크립트로 실행하기 (간단한 entry point)

`pyproject.toml` 에 `[project.scripts]` 추가하면 `uv run pmon` 같이 호출 가능:

```toml
[project.scripts]
pmon = "pmon:main"
```

단, 현재 `pmon.py` 는 script-style (모듈 import 없이 직접 실행)이라 그대로는 안 됨. 패키지화하려면:
1. `pmon.py` 의 모든 코드를 `if __name__ == "__main__":` 블록 밖으로 빼서 `main()` 정의
2. `[project.scripts]` 등록

지금은 그냥 `uv run ./pmon.py ...` 형태가 가장 간단.

## 7. CI / 다른 시스템에서 reproduce

```bash
# 깨끗한 clone 후:
git clone <repo> performance && cd performance
uv sync                          # .venv 재현 (deps 같은 버전)
uv run ./pmon.py debug           # 회귀 검증
```

`uv.lock` 이 추적되어 있으므로 다른 시스템에서도 동일한 Python + 같은 deps 버전 보장.

## 8. .venv 정리

```bash
rm -rf .venv                # 통째로 삭제
uv sync                     # 재생성

# 또는 uv가 관리하는 Python 자체 정리
uv python uninstall 3.12
uv python install 3.12
```

## 9. 자주 쓰는 워크플로 예

**경우 A**: 새 SSD 도착, 기본 성능 측정 + 리포트 확인
```bash
uv sync
uv run ./pmon.py monitor --fio "fio --name=baseline --filename=/dev/nvme1n1 \
    --rw=randread --bs=4k --iodepth=128 --numjobs=4 --runtime=60 \
    --time_based --direct=1 --ioengine=libaio --group_reporting"
xdg-open results/*/report_*.pdf      # PDF 리포트 (차트 포함)
```

**경우 B**: 변경 전후 성능 비교
```bash
# 변경 전
uv run ./pmon.py monitor --fio "fio --name=before --filename=/dev/nvme1n1 \
    --rw=randrw --rwmixread=70 --bs=4k --iodepth=64 --runtime=30 \
    --time_based --direct=1 --ioengine=libaio --numjobs=2 --group_reporting"
BASELINE=$(ls -t results/topology_*.json | head -1 | grep -oE "[0-9]{8}_[0-9]{6}")

# (코드/설정/펌웨어 변경)

# 변경 후 (같은 fio 명령)
uv run ./pmon.py monitor --fio "fio --name=after --filename=/dev/nvme1n1 \
    --rw=randrw --rwmixread=70 --bs=4k --iodepth=64 --runtime=30 \
    --time_based --direct=1 --ioengine=libaio --numjobs=2 --group_reporting"
CAND=$(ls -t results/topology_*.json | head -1 | grep -oE "[0-9]{8}_[0-9]{6}")

# 비교
uv run ./pmon.py diff --baseline "$BASELINE" --candidate "$CAND"
cat results/diff_${BASELINE}_vs_${CAND}.md
```

**경우 C**: sub-second sampling (transient spike 잡기)
```bash
uv run ./pmon.py monitor --ebpf-interval 0.2 --fio "fio --name=spike \
    --filename=/dev/nvme1n1 --rw=randwrite --bs=4k --iodepth=32 \
    --runtime=15 --time_based --direct=1 --ioengine=libaio \
    --numjobs=2 --group_reporting"
# 75개 sample (15s / 0.2s) — 차트 더 촘촘
```

**경우 D**: GPU compute + 동시 fio (PCIe 경합 측정)
```bash
uv run python -m workloads.scenarios.pcie_contention
# CUDA loader 빌드 → GPU memcpy bounce 동시 실행 + fio
# analyze 결과로 exit code (pass/fail)
```

**경우 E**: 오프라인/GUI 없는 서버 — 정적 PNG + Markdown 리포트
```bash
uv run python -m report.png_report
# 출력:
#   results/report_png_<sid>.md
#   results/figs_<sid>/*.png  (11+ 차트)

# 또는 --format 으로
uv run python -m report --format png

# 측정 + PNG 리포트 같이
uv run ./pmon.py monitor --fio "..." --report png

# md만 보고 싶으면 (이미지 안 보임, less로 OK)
less results/report_png_<sid>.md

# tarball로 묶어 다른 머신으로 옮기기
tar czf /tmp/perf_report.tar.gz -C results \
    report_png_<sid>.md figs_<sid>
```

PNG는 matplotlib (Agg backend) 사용 — X11/GUI 필요 없음. Markdown 렌더러
(vscode, github, mdcat 등) 어디서든 PNG 자동 표시.

**경우 F**: 멀티 디바이스 동시 측정
```bash
# 한 fio 명령으로 여러 디바이스 묶기 (filename1:filename2):
uv run ./pmon.py monitor --fio "fio --name=multi \
    --filename=/dev/nvme0n1:/dev/nvme1n1:/dev/nvme2n1 \
    --rw=randread --bs=4k --iodepth=64 --runtime=20 --time_based \
    --direct=1 --ioengine=libaio --numjobs=4 --group_reporting"
# 리포트 Overview 패널에 3 device 라인 자동 overlay
```

## 10. 트러블슈팅

| 증상 | 해결 |
|---|---|
| `uv: command not found` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` 로 설치 후 `source ~/.bashrc` |
| `uv run` 이 다른 Python 버전 사용 | `cat .python-version` 확인. 필요시 `uv python pin 3.12` 재실행 |
| `.venv` 가 깨졌다 (이상한 에러) | `rm -rf .venv && uv sync` |
| pmon.py 가 sudo 안 됨 | sudoers NOPASSWD에 `monitoring/collectors/ebpf_io/src/io_trace` 등록 필요. `DEV_RULES.md` 참고 |
| `python3 -m report` 가 ModuleNotFoundError | 프로젝트 루트 cwd에서 호출해야 함. 절대 경로면 `cd /path/to/performance && uv run python -m report` |

## 11. 명령 한 줄 정리

```bash
# 셋업
cd /home/cm/src/cm/performance && uv sync

# 측정 + 리포트
uv run ./pmon.py monitor --fio "fio ..."

# 리포트만
uv run ./pmon.py report

# 비교
uv run ./pmon.py diff --baseline SID1 --candidate SID2

# 시나리오
uv run python -m workloads.scenarios.sample_randread

# smoke
./pmon.py debug

# 의존성 추가
uv add <package>

# 환경 재생성
rm -rf .venv && uv sync
```
