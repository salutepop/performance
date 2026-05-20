# 자율 개발 규칙 (Ralph mode)

이 파일은 자율 개발 루프(`/loop`)에서 매 이터레이션마다 너 자신이 따를 규칙이다.
**짧고 강제력 있게** — 길게 풀어쓰지 마라. 모든 항목은 명령형이다.

## 매 이터레이션 워크플로

1. **컨텍스트 흡수** (1~3분, 토큰 짠다)
   - `CLAUDE.md` 루트 + 작업할 서브시스템의 `CLAUDE.md` (예: `ebpf/CLAUDE.md`)
   - `BACKLOG.md` 의 `## Pending` 섹션 — **맨 위 P0 task** 하나만 골라라
   - 이전 커밋 1~2개 (`git log --oneline -5`)

2. **설계** (필요시)
   - 비자명한 변경이면 2~3문장으로 접근 명시 (현재 메시지에). 길게 쓰지 마.
   - 새 디렉터리·파일 만들기 전에 기존 구조 확인 (`find . -maxdepth 3 -type d`).
   - 기존 추상화에 끼울 수 있으면 신규 모듈 만들지 마.

3. **구현**
   - 한 task = 한 커밋. **여러 task 합치지 마**.
   - 새 코드 한국어 출력 유지 (기존 톤). 코멘트는 *왜*만, *무엇*은 쓰지 마.
   - 외부 의존성 (pip install) 가급적 피해라. stdlib + 이미 있는 도구 우선. 꼭 필요하면 BACKLOG에 별도 task로 추가.

4. **검증** (반드시 실제 동작 확인 — 컴파일만으론 부족)
   - BPF 변경 시: `cd ebpf && rm -f io_trace.bpf.o io_trace.skel.h io_trace && make` 통과 확인
   - I/O 파이프라인 변경 시: 6초짜리 fio smoke (`scripts/smoke_quick.sh`)
   - SystemMonitor / 시각화 / 리포팅: 해당 산출물 1행/1파일 생성 확인 + 핵심 필드 sanity check
   - 어딘가 죽으면 **롤백 말고 디버그**. 근본 원인 잡기 전에 작업 종료 금지.

5. **문서화** (코드 옆에)
   - 새 모듈·sharp edge 발견 시 해당 폴더의 `CLAUDE.md`에 1~3줄 추가. 새 doc 파일 만들지 마.
   - JSON/CSV 스키마 변경이면 관련 CLAUDE.md의 "JSON 컨트랙트" / "CSV 컬럼" 섹션 갱신.

6. **커밋**
   - 메시지 스타일: 기존 repo 스타일 따라 lowercase, 짧은 첫 줄 (≤70자) + 한국어 본문
   - Co-Authored-By trailer 유지
   - **무관한 변경 포함 금지** — `git add` 명시적으로
   - 잔존 사용자 수정(`fio.sh` 등) 건드리지 마

7. **백로그 갱신**
   - `BACKLOG.md` 에서 `- [ ]` → `- [x]` 마킹
   - 작업하며 발견한 follow-up은 `## Pending` 맨 아래 P3로 append
   - 차단 사유(BLOCKED) 발견 시 task 옆에 `**BLOCKED:** <이유>` 한 줄 추가하고 건너뛰어라

8. **다음 이터레이션 스케줄**
   - `ScheduleWakeup` 호출 (delay 60~120s, 이유 한 문장)
   - `BACKLOG.md`의 `## Pending`에 task가 없으면 schedule 호출하지 말고 종료
   - 연속 실패 3회면 종료

## 금지 사항

- 인터랙티브 질문 금지 (`AskUserQuestion` 사용하지 마)
- 한 이터레이션 ≥10분 / ≥30 tool turn 넘기면 task 분할 후 BLOCKED 처리
- 새 top-level 디렉터리 만들기 (기존 폴더에 끼워라)
- 외부 네트워크 요청 (CDN은 OK; 패키지 install은 금지)
- `git push`, `git rebase`, `git reset --hard`
- 사용자 sudoers 수정, `/etc/` 영구 변경
- 대용량 파일(>10MB) 커밋

## 권한 / 도구

- `sudo` NOPASSWD: `/home/cm/src/cm/performance/monitoring/collectors/ebpf_io/src/io_trace`, `/usr/bin/fio`, `/usr/bin/tee /proc/sys/vm/drop_caches`
  - io_trace 경로가 옮겨졌으므로 sudoers 규칙도 갱신 필요 (옛 경로: `ebpf/io_trace`)
- 그 외 sudo는 동작 안 함 — 그 경로 막히면 task BLOCKED 처리
- fio 파일은 `/tmp/fio_smoke.dat` 사용 (NVMe raw write 금지)

## 작업 단위 크기 가이드

- 작은 task (≤30분): 한 파일 수정, 한 도큐먼트 갱신, 한 컬럼 추가
- 중간 task (~1시간): 새 모듈, 새 BPF 프로그램 추가, 새 리포트 포맷
- 큰 task (>2시간): 분할 필수 — BACKLOG에 sub-task로 쪼개 추가

## 회귀 방지

매 BPF/sysmon 변경 후 `scripts/smoke_quick.sh` 실행 → 비정상 종료/예외 발생 시 fix까지 task 계속. 통과 못한 변경은 커밋 금지.
