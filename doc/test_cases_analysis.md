# Test Case 분석 및 신규 TC 설계 제안

현재 프로젝트에 포함된 테스트 케이스(TC)들을 분석하고, 이를 바탕으로 추가적으로 필요한 테스트 시나리오를 제안합니다.

## 🔍 기존 Test Case 분석

| TC ID | 형식 | 주요 목적 | 주요 설정 | 특징 |
| :--- | :--- | :--- | :--- | :--- |
| **TC01** | JSON | 기초 성능 측정 | Seq Read(128k, QD32), Rand Write(4k, QD128) | 가장 기본적인 대역폭 및 IOPS 확인용 |
| **TC02** | Python | Dirty State 및 GC 영향 분석 | Preconditioning(1M Write) -> Cache Drop -> Rand Read(4k, QD1) | SSD 내부의 Garbage Collection 동작 및 지연시간 이상 현상 감지 |
| **TC03** | Python | 코어별 NUMA Distance 측정 | 전 코어 순회하며 Rand Read(4k, QD1) 수행 | CPU 코어 위치에 따른 지연시간 편차 확인 및 테이블 리포트 생성 |
| **TC04** | Python | 단일 코어 최대 IOPS 측정 | 전 코어 순회하며 Rand Read(4k, QD64) 수행 | 특정 코어가 낼 수 있는 최대 성능 한계 측정 |
| **TC05** | Python | 단일 코어 최소 지연시간 측정 | 전 코어 순회하며 Rand Read(4k, QD1) 수행 | QD1 환경에서 각 코어의 순수 응답 속도 비교 (TC03과 유사하나 지표 집중) |

> [!NOTE]
> **TC05 개선 필요 사항**: 코드 내 일부 라벨이 TC04의 'QD64'로 잘못 기재되어 있어, 'QD1'에 맞는 라벨로 수정이 필요합니다.

---

## 💡 신규 Test Case (TC) 작성 제안

SSD의 성능을 보다 입체적으로 분석하기 위해 다음과 같은 시나리오 추가를 제안합니다.

### 1. TC06: Mixed Workload (70:30)
- **목적**: 실제 서버 환경에서 가장 빈번하게 발생하는 읽기/쓰기 혼합 부하 성능 측정.
- **내용**: 4K Random Read 70% + Random Write 30% 혼합 부하를 QD1부터 QD256까지 가변하며 측정.

### 2. TC07: SLC Cache Recovery & Sustained Performance
- **목적**: SLC 캐시 소모 시점과 성능 하락폭, 그리고 휴지기 후 캐시 회복 속도 측정.
- **내용**: 
  1. 디스크 전체 용량의 50%를 순차 쓰기로 채우며 실시간 대역폭 기록.
  2. 쓰기 중단 후 일정 시간(10초, 30초, 60초) 대기.
  3. 다시 쓰기를 수행하여 성능이 원래대로 돌아오는지 확인.

### 3. TC08: Latency Consistency (Jitter Analysis)
- **목적**: 장시간 부하 시 발생하는 지연시간의 튀는 현상(Tail Latency) 분석.
- **내용**: 10분 이상 Random Write 부하를 주면서 99.9th, 99.99th Percentile Latency 변화 추이 기록.

### 4. TC09: Block Size Sweep
- **목적**: 데이터 블록 크기(BS) 변화에 따른 처리 효율성 비교.
- **내용**: 512B, 4K, 8K, 16K, 32K, 64K, 128K, 1M 순으로 BS를 변경하며 BW/IOPS 상관관계 도출.

---

## 🛠 신규 TC 작성을 위한 준비 작업
다음 단계로 진행하기 위해 아래 작업들을 수행할 예정입니다.
1. **TC05 버그 수정**: 잘못된 라벨 및 설명 문구 수정.
2. **TC06 (Mixed Workload) 템플릿 작성**: Python 기반의 가변 QD 측정 시나리오 구현.
3. **결과 시각화 스크립트 검토**: 수집된 JSON 데이터를 그래프(Line Chart)로 그릴 수 있는 파서(Parser) 기능 정의.
