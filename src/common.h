/* Shared helpers: CPU pinning, timing, huge-page allocation, stats. */
#ifndef FT_COMMON_H
#define FT_COMMON_H

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define CACHELINE 64
#define HUGE_2M (2UL << 20)

#define die(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); exit(2); } while (0)

static inline uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static inline int64_t ts_ns(const struct timespec *ts)
{
	return (int64_t)ts->tv_sec * 1000000000LL + ts->tv_nsec;
}

static inline void cpu_relax(void)
{
	__builtin_ia32_pause();
}

static inline void pin_self(int cpu)
{
	cpu_set_t set;
	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	int rc = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
	if (rc)
		die("cannot pin to cpu %d: %s", cpu, strerror(rc));
}

/* Parse "0-3,8,10-11" into cpus[]; returns the count. */
static inline int parse_cpulist(const char *s, int *cpus, int max)
{
	int n = 0;
	while (*s) {
		char *end;
		long a = strtol(s, &end, 10), b = a;
		if (end == s)
			die("bad cpu list near '%s'", s);
		if (*end == '-')
			b = strtol(end + 1, &end, 10);
		for (long c = a; c <= b && n < max; c++)
			cpus[n++] = (int)c;
		s = (*end == ',') ? end + 1 : end;
		if (*end && *end != ',')
			die("bad cpu list near '%s'", end);
	}
	return n;
}

/* Online CPUs from the current affinity mask. */
static inline int online_cpus(int *cpus, int max)
{
	cpu_set_t set;
	int n = 0;
	sched_getaffinity(0, sizeof(set), &set);
	for (int c = 0; c < CPU_SETSIZE && n < max; c++)
		if (CPU_ISSET(c, &set))
			cpus[n++] = c;
	return n;
}

/* Parse "16m", "2g", "512k" or plain bytes. */
static inline size_t parse_size(const char *s)
{
	char *end;
	double v = strtod(s, &end);
	switch (*end | 0x20) {
	case 'k': v *= 1UL << 10; break;
	case 'm': v *= 1UL << 20; break;
	case 'g': v *= 1UL << 30; break;
	}
	return (size_t)v;
}

/*
 * Allocate len bytes, preferring reserved 2 MB hugetlb pages and falling
 * back to THP. *kind names what we got so reports can flag TLB effects.
 */
static inline void *huge_alloc(size_t len, const char **kind)
{
	len = (len + HUGE_2M - 1) & ~(HUGE_2M - 1);
	void *p = mmap(NULL, len, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB | MAP_POPULATE, -1, 0);
	if (p != MAP_FAILED) {
		*kind = "hugetlb-2M";
		return p;
	}
	if (posix_memalign(&p, HUGE_2M, len))
		die("out of memory allocating %zu bytes", len);
	madvise(p, len, MADV_HUGEPAGE);
	memset(p, 0, len);
	*kind = "thp pages";
	return p;
}

static inline uint64_t xorshift64(uint64_t *s)
{
	uint64_t x = *s;
	x ^= x << 13;
	x ^= x >> 7;
	x ^= x << 17;
	return *s = x;
}

/*
 * Link every cache line of buf into one random cycle so each load depends on
 * the previous one and the prefetchers can't guess the next address.
 */
static inline void **build_chase(void *buf, size_t len, uint64_t seed)
{
	size_t n = len / CACHELINE;
	size_t *order = malloc(n * sizeof(*order));
	if (!order)
		die("out of memory building chase of %zu lines", n);
	for (size_t i = 0; i < n; i++)
		order[i] = i;
	for (size_t i = n - 1; i > 0; i--) {
		size_t j = xorshift64(&seed) % (i + 1);
		size_t t = order[i]; order[i] = order[j]; order[j] = t;
	}
	char *b = buf;
	for (size_t i = 0; i < n; i++)
		*(void **)(b + order[i] * CACHELINE) = b + order[(i + 1) % n] * CACHELINE;
	void **head = (void **)(b + order[0] * CACHELINE);
	free(order);
	return head;
}

static inline void **chase(void **p, size_t loads)
{
	while (loads >= 8) {
		p = *p; p = *p; p = *p; p = *p;
		p = *p; p = *p; p = *p; p = *p;
		loads -= 8;
	}
	while (loads--)
		p = *p;
	return p;
}

static int cmp_double(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;
	return (x > y) - (x < y);
}

/* Sorts v in place; returns the q-quantile (0..1). */
static inline double quantile(double *v, size_t n, double q)
{
	if (!n)
		return 0;
	qsort(v, n, sizeof(*v), cmp_double);
	size_t i = (size_t)(q * (n - 1) + 0.5);
	return v[i];
}

#endif
