#!/bin/bash
# 1초 간격으로 모든 코어의 CPU 통계를 파일에 저장 (IRQ 확인용)
mpstat -P ALL 1 >"$1"
