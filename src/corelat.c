/*
 * corelat - rank candidate hot cores relative to the poller core.
 *
 * For every target CPU it measures:
 *   c2c_ns_from_base  one-way cache-line transfer, base CPU -> target
 *   chase_ns          dependent-load latency over -s bytes (default 16 MiB = L3)
 *   clock_GHz         sustained clock under a dependent-multiply loop
 *
 * Output is sorted best-first: lowest c2c, then lowest chase, then highest clock.
 */
#include "common.h"

#include <getopt.h>

#define PINGPONG_ROUNDS 20000
#define PINGPONG_TRIALS 7

struct shared_line {
	volatile uint64_t v;
} __attribute__((aligned(CACHELINE)));

static struct shared_line line;
static void **chase_head;
static size_t chase_lines;

struct result {
	int cpu;
	int l3_id;
	int smt_sibling;
	double c2c_ns;
	double chase_ns;
	double ghz;
};

struct worker {
	int cpu;
	struct result *res;
};

static void *pong(void *arg)
{
	struct worker *w = arg;
	pin_self(w->cpu);

	for (int t = 0; t < PINGPONG_TRIALS; t++) {
		for (uint64_t i = 0; i < PINGPONG_ROUNDS; i++) {
			uint64_t want = (uint64_t)t * PINGPONG_ROUNDS * 2 + i * 2 + 1;
			while (line.v != want)
				cpu_relax();
			line.v = want + 1;
		}
	}
	return NULL;
}

static double measure_c2c(int base, int cpu)
{
	struct worker w = { .cpu = cpu };
	pthread_t th;
	double trials[PINGPONG_TRIALS];

	line.v = 0;
	pin_self(base);
	pthread_create(&th, NULL, pong, &w);
	for (int t = 0; t < PINGPONG_TRIALS; t++) {
		uint64_t t0 = now_ns();
		for (uint64_t i = 0; i < PINGPONG_ROUNDS; i++) {
			uint64_t send = (uint64_t)t * PINGPONG_ROUNDS * 2 + i * 2 + 1;
			line.v = send;
			while (line.v != send + 1)
				cpu_relax();
		}
		/* A round trip is two one-way transfers. */
		trials[t] = (double)(now_ns() - t0) / PINGPONG_ROUNDS / 2;
	}
	pthread_join(th, NULL);
	return quantile(trials, PINGPONG_TRIALS, 0.5);
}

static void *measure_on_core(void *arg)
{
	struct worker *w = arg;
	pin_self(w->cpu);

	size_t loads = chase_lines * 2;
	if (loads < (1 << 20))
		loads = 1 << 20;
	if (loads > (4 << 20))
		loads = 4 << 20;

	void **p = chase(chase_head, chase_lines < loads ? chase_lines : loads);
	double best = 1e9;
	for (int r = 0; r < 3; r++) {
		uint64_t t0 = now_ns();
		p = chase(p, loads);
		double ns = (double)(now_ns() - t0) / loads;
		if (ns < best)
			best = ns;
	}
	w->res->chase_ns = best;
	__asm__ volatile("" : : "r"(p));

	/*
	 * Spin ~50 ms so the core reaches its sustained clock, then time a chain
	 * of 64 dependent imuls (3-cycle latency on current Intel and AMD cores).
	 * Not adds: newer Intel cores fold dependent add-immediate chains at rename.
	 */
	uint64_t x = 1, m = 1, iters = 0;
	uint64_t t0 = now_ns(), t1;
	while (now_ns() - t0 < 50000000ULL)
		__asm__ volatile(".rept 64\n\timul %1, %0\n\t.endr" : "+r"(x) : "r"(m));
	t0 = now_ns();
	do {
		for (int i = 0; i < 1000; i++)
			__asm__ volatile(".rept 64\n\timul %1, %0\n\t.endr" : "+r"(x) : "r"(m));
		iters += 1000;
		t1 = now_ns();
	} while (t1 - t0 < 100000000ULL);
	w->res->ghz = (double)iters * 64 * 3 / (double)(t1 - t0);
	return NULL;
}

static int read_int_file(const char *fmt, int cpu)
{
	char path[256];
	snprintf(path, sizeof(path), fmt, cpu);
	FILE *f = fopen(path, "r");
	int v = -1;
	if (f) {
		if (fscanf(f, "%d", &v) != 1)
			v = -1;
		fclose(f);
	}
	return v;
}

static int is_smt_sibling(int base, int cpu)
{
	char path[256], buf[256];
	snprintf(path, sizeof(path),
		 "/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list", base);
	FILE *f = fopen(path, "r");
	if (!f || !fgets(buf, sizeof(buf), f)) {
		if (f)
			fclose(f);
		return 0;
	}
	fclose(f);
	buf[strcspn(buf, "\n")] = 0;
	int sib[64], n = parse_cpulist(buf, sib, 64);
	for (int i = 0; i < n; i++)
		if (sib[i] == cpu)
			return 1;
	return 0;
}

static int cmp_result(const void *a, const void *b)
{
	const struct result *x = a, *y = b;
	if (x->c2c_ns != y->c2c_ns)
		return x->c2c_ns < y->c2c_ns ? -1 : 1;
	if (x->chase_ns != y->chase_ns)
		return x->chase_ns < y->chase_ns ? -1 : 1;
	return (x->ghz < y->ghz) - (x->ghz > y->ghz);
}

static void usage(void)
{
	fprintf(stderr,
		"usage: corelat -b BASE_CPU [-c CPULIST] [-s SIZE]\n"
		"  -b  poller CPU that every target is measured from\n"
		"  -c  candidate hot cores (default: every other allowed CPU)\n"
		"  -s  pointer-chase working set (default 16m; 2g measures DRAM)\n");
	exit(2);
}

int main(int argc, char **argv)
{
	int base = -1, cpus[CPU_SETSIZE], ncpus = 0;
	size_t size = 16UL << 20;
	int opt;

	while ((opt = getopt(argc, argv, "b:c:s:h")) != -1) {
		switch (opt) {
		case 'b': base = atoi(optarg); break;
		case 'c': ncpus = parse_cpulist(optarg, cpus, CPU_SETSIZE); break;
		case 's': size = parse_size(optarg); break;
		default: usage();
		}
	}
	if (base < 0)
		usage();
	if (!ncpus)
		ncpus = online_cpus(cpus, CPU_SETSIZE);

	const char *kind;
	void *buf = huge_alloc(size, &kind);
	chase_lines = size / CACHELINE;
	chase_head = build_chase(buf, size, 0x9e3779b97f4a7c15ULL);

	struct result *res = calloc(ncpus, sizeof(*res));
	int n = 0;
	for (int i = 0; i < ncpus; i++) {
		int cpu = cpus[i];
		if (cpu == base)
			continue;
		struct result *r = &res[n++];
		r->cpu = cpu;
		r->l3_id = read_int_file("/sys/devices/system/cpu/cpu%d/cache/index3/id", cpu);
		r->smt_sibling = is_smt_sibling(base, cpu);
		r->c2c_ns = measure_c2c(base, cpu);

		struct worker w = { .cpu = cpu, .res = r };
		pthread_t th;
		pthread_create(&th, NULL, measure_on_core, &w);
		pthread_join(th, NULL);
		fprintf(stderr, "\rmeasured cpu %d (%d/%d)   ", cpu, n, ncpus - 1);
	}
	fprintf(stderr, "\r%40s\r", "");

	qsort(res, n, sizeof(*res), cmp_result);
	printf("%5s %6s %16s %9s %9s  %s\n",
	       "cpu", "l3_id", "c2c_ns_from_base", "chase_ns", "clock_GHz", "note");
	for (int i = 0; i < n; i++) {
		struct result *r = &res[i];
		printf("%5d %6d %16.1f %9.1f %9.2f  %s\n", r->cpu, r->l3_id, r->c2c_ns,
		       r->chase_ns, r->ghz, r->smt_sibling ? "smt-sibling-of-base" : "");
	}
	printf("# base=%d l3_id=%d chase=%zu MiB (%s)\n", base,
	       read_int_file("/sys/devices/system/cpu/cpu%d/cache/index3/id", base),
	       size >> 20, kind);
	return 0;
}
