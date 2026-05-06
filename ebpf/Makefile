APP = io_trace
BPF_OBJ = $(APP).bpf.o
SKEL_H = $(APP).skel.h
ARCH := $(shell uname -m | sed 's/x86_64/x86/' | sed 's/aarch64/arm64/')

.PHONY: all clean run

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

# 정리
clean:
	rm -f $(APP) $(BPF_OBJ) $(SKEL_H)
