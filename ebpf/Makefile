APP = io_trace
BPF_OBJ = $(APP).bpf.o
SKEL_H = $(APP).skel.h
ARCH := $(shell uname -m | sed 's/x86_64/x86/' | sed 's/aarch64/arm64/')

.PHONY: all clean run smoke

all: $(APP)

# vmlinux.h 생성
vmlinux.h:
	bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h

# BPF 오브젝트 파일 컴파일
$(BPF_OBJ): $(APP).bpf.c vmlinux.h
	clang -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) -I. -c $< -o $@

# 스켈레톤 헤더 생성
$(SKEL_H): $(BPF_OBJ)
	bpftool gen skeleton $< > $@

# 최종 바이너리 빌드
$(APP): $(APP).c $(SKEL_H)
	gcc -g -O2 $< -o $@ -lbpf -lelf -lz

# 실행
run: $(APP)
	sudo ./$(APP)

# BPF 개발 워크플로: bpf 코드 수정 → 빌드 → tc00 스모크로 attach+측정+CSV+리포트 전체 sanity.
# (Q2D/D2C 값이 0이 아니고 device CSV가 채워지면 정상. fio raw + sysmon + eBPF + report 자동 모두 검증)
smoke: $(APP)
	cd .. && python3 main.py -t tc00 -q

# 정리
clean:
	rm -f $(APP) $(BPF_OBJ) $(SKEL_H)