#!/bin/bash

SIZE="1G"
TARGET="./fio_test_file.dat"

echo "=========================================="
echo " [*] FIO Benchmark Workload Started"
echo "=========================================="

#fio --name=seq_write --ioengine=libaio --rw=write --bs=1M --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --time_based --runtime=60 --filename=$TARGET
echo "[1/4] Running Sequential Write (1M)..."
fio --name=seq_write --ioengine=libaio --rw=write --bs=1M --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET

echo "[2/4] Running Sequential Read (1M)..."
fio --name=seq_read --ioengine=libaio --rw=read --bs=1M --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET

#echo "[3/4] Running Random Write (4K)..."
#fio --name=rand_write --ioengine=libaio --rw=randwrite --bs=4k --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET

#echo "[4/4] Running Random Read (4K)..."
#fio --name=rand_read --ioengine=libaio --rw=randread --bs=4k --size=$SIZE --numjobs=1 --iodepth=32 --direct=1 --filename=$TARGET

rm -f $TARGET

echo "=========================================="
echo " [*] FIO Benchmark Completed!"
echo "=========================================="
