#!/bin/bash

# 테스트할 더미 파일 크기 설정
SIZE="1G"
# 테스트 파일 이름 (현재 디렉토리에 생성됨)
TARGET="./fio_test_file.dat"

echo "=========================================="
echo " [*] FIO Benchmark Workload Started"
echo "=========================================="

# 1. Sequential Write (순차 쓰기 - 1M 블록)
echo "[1/4] Running Sequential Write (1M)..."
fio --name=seq_write --ioengine=libaio --rw=write --bs=1M --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET >/dev/null

# 2. Sequential Read (순차 읽기 - 1M 블록)
echo "[2/4] Running Sequential Read (1M)..."
fio --name=seq_read --ioengine=libaio --rw=read --bs=1M --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET >/dev/null

# 3. Random Write (랜덤 쓰기 - 4K 블록)
echo "[3/4] Running Random Write (4K)..."
fio --name=rand_write --ioengine=libaio --rw=randwrite --bs=4k --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET >/dev/null

# 4. Random Read (랜덤 읽기 - 4K 블록)
echo "[4/4] Running Random Read (4K)..."
fio --name=rand_read --ioengine=libaio --rw=randread --bs=4k --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET >/dev/null

# 테스트 완료 후 더미 파일 삭제 (프로파일링 중 플러시/디스카드가 발생할 수 있음)
rm -f $TARGET

echo "=========================================="
echo " [*] FIO Benchmark Completed!"
echo "=========================================="
