CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra -march=x86-64-v2
LDLIBS  += -pthread

BINS := bin/corelat bin/llcpollute bin/wire2app

all: $(BINS)

bin/%: src/%.c src/common.h src/fastcopy.h | bin
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

bin:
	mkdir -p bin

# One-shot host setup: packages, msr, huge pages, build.
setup:
	sudo HUGEPAGES=$${HUGEPAGES:-2048} scripts/prereqs.sh install

clean:
	rm -rf bin

.PHONY: all clean setup
