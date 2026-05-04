#!/bin/bash
# 1초 간격으로 디스크 확장 통계를 파일에 저장
iostat -dxm 1 >"$1"
