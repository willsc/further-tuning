/*
 * llcpollute - how much does a logger's copy loop hurt the hot core's L3 hits?
 *
 * A victim thread pointer-chases its working set (-l) on the hot core and
 * records ns/load per batch of 10,000 loads. Meanwhile a copier thread on
 * another core copies a large buffer (-s) with each method in turn:
 *
 *   idle     copier does nothing (baseline)
 *   memcpy   ordinary copy, allocates in cache
 *   nt       prefetchnta + streaming stores (nt_copy)
 *   dsa      Intel DSA memmove, cache-control off: writes go to memory
 *   dsa-llc  Intel DSA memmove, cache-control on: writes allocate in L3
 */
#include "common.h"
#include "fastcopy.h"

#include <getopt.h>
#include <stdatomic.h>

#define BATCH_LOADS 10000
#define CHUNK (256UL << 10)
#define MAX_BATCHES (1 << 22)

enum method { M_IDLE, M_MEMCPY, M_NT, M_DSA, M_DSA_LLC, M_COUNT };
static const char *method_names[M_COUNT] = { "idle", "memcpy", "nt", "dsa", "dsa-llc" };

static atomic_int running;
static atomic_int copier_ready;

struct victim {
	int cpu;
	void **head;
	size_t lines;
	double *batches;
	size_t nbatches;
};

struct copier {
	int cpu;
	enum method m;
	char *src, *dst;
	size_t size;
	struct dsa_wq *wq;
	uint64_t bytes;
	uint64_t elapsed_ns;
	int failed;
};

static void *victim_run(void *arg)
{
	struct victim *v = arg;
	pin_self(v->cpu);

	/* Pull the working set into cache before measuring. */
	void **p = chase(v->head, v->lines * 2);
	while (!atomic_load(&copier_ready))
		cpu_relax();

	v->nbatches = 0;
	while (atomic_load_explicit(&running, memory_order_relaxed) &&
	       v->nbatches < MAX_BATCHES) {
		uint64_t t0 = now_ns();
		p = chase(p, BATCH_LOADS);
		v->batches[v->nbatches++] = (double)(now_ns() - t0) / BATCH_LOADS;
	}
	__asm__ volatile("" : : "r"(p));
	return NULL;
}

static void *copier_run(void *arg)
{
	struct copier *c = arg;
	pin_self(c->cpu);
	atomic_store(&copier_ready, 1);

	uint64_t t0 = now_ns();
	size_t off = 0;
	while (atomic_load_explicit(&running, memory_order_relaxed)) {
		char *d = c->dst + off;
		const char *s = c->src + off;
		switch (c->m) {
		case M_IDLE:
			usleep(1000);
			continue;
		case M_MEMCPY:
			memcpy(d, s, CHUNK);
			__asm__ volatile("" : : "r"(d) : "memory");
			break;
		case M_NT:
			nt_copy(d, s, CHUNK);
			break;
		case M_DSA:
		case M_DSA_LLC:
			if (dsa_memmove(c->wq, d, s, CHUNK, c->m == M_DSA_LLC)) {
				c->failed = 1;
				atomic_store(&running, 0);
				return NULL;
			}
			break;
		default:
			break;
		}
		c->bytes += CHUNK;
		off += CHUNK;
		if (off + CHUNK > c->size)
			off = 0;
	}
	c->elapsed_ns = now_ns() - t0;
	return NULL;
}

static void usage(void)
{
	fprintf(stderr,
		"usage: llcpollute -v HOT_CPU -c COPIER_CPU [-l SIZE] [-s SIZE] [-t SEC]\n"
		"                  [-m METHODS] [-w /dev/dsa/wqX.Y]\n"
		"  -v  hot (victim) core\n"
		"  -c  copier core: same socket (Intel) or CCD (AMD) as -v\n"
		"  -l  victim working set, > L2 (default 8m)\n"
		"  -s  copier buffer size, well above L3 (default 512m)\n"
		"  -t  seconds per method (default 5)\n"
		"  -m  comma list from idle,memcpy,nt,dsa,dsa-llc (default all available)\n"
		"  -w  DSA user work queue (default /dev/dsa/wq0.0)\n");
	exit(2);
}

int main(int argc, char **argv)
{
	int vcpu = -1, ccpu = -1, secs = 5, opt;
	size_t lsize = 8UL << 20, ssize = 512UL << 20;
	const char *wq_path = "/dev/dsa/wq0.0", *mlist = NULL;

	while ((opt = getopt(argc, argv, "v:c:l:s:t:m:w:h")) != -1) {
		switch (opt) {
		case 'v': vcpu = atoi(optarg); break;
		case 'c': ccpu = atoi(optarg); break;
		case 'l': lsize = parse_size(optarg); break;
		case 's': ssize = parse_size(optarg); break;
		case 't': secs = atoi(optarg); break;
		case 'm': mlist = optarg; break;
		case 'w': wq_path = optarg; break;
		default: usage();
		}
	}
	if (vcpu < 0 || ccpu < 0)
		usage();

	struct dsa_wq wq;
	int have_dsa = dsa_open(&wq, wq_path) == 0;

	int want[M_COUNT] = { 0 };
	if (mlist) {
		char *s = strdup(mlist);
		for (char *tok = strtok(s, ","); tok; tok = strtok(NULL, ",")) {
			int found = 0;
			for (int m = 0; m < M_COUNT; m++)
				if (!strcmp(tok, method_names[m]))
					want[m] = found = 1;
			if (!found)
				die("unknown method '%s'", tok);
		}
		free(s);
		if ((want[M_DSA] || want[M_DSA_LLC]) && !have_dsa)
			die("cannot open DSA work queue %s: %s", wq_path, strerror(errno));
	} else {
		want[M_IDLE] = want[M_MEMCPY] = want[M_NT] = 1;
		want[M_DSA] = want[M_DSA_LLC] = have_dsa;
		if (!have_dsa)
			fprintf(stderr, "no DSA work queue at %s; running idle, memcpy, nt only\n",
				wq_path);
	}

	const char *vkind, *ckind;
	void *vbuf = huge_alloc(lsize, &vkind);
	struct victim v = {
		.cpu = vcpu,
		.head = build_chase(vbuf, lsize, 0x2545f4914f6cdd1dULL),
		.lines = lsize / CACHELINE,
		.batches = malloc(MAX_BATCHES * sizeof(double)),
	};
	char *src = huge_alloc(ssize, &ckind);
	char *dst = huge_alloc(ssize, &ckind);
	memset(src, 0x5a, ssize);
	memset(dst, 0, ssize);

	printf("%-8s %14s %14s %14s %12s\n",
	       "method", "victim_p50_ns", "victim_p99_ns", "victim_max_ns", "copier_GB/s");
	for (int m = 0; m < M_COUNT; m++) {
		if (!want[m])
			continue;
		struct copier c = {
			.cpu = ccpu, .m = m, .src = src, .dst = dst, .size = ssize,
			.wq = have_dsa ? &wq : NULL,
		};
		pthread_t vt, ct;
		atomic_store(&running, 1);
		atomic_store(&copier_ready, 0);
		pthread_create(&ct, NULL, copier_run, &c);
		pthread_create(&vt, NULL, victim_run, &v);
		for (int i = 0; i < secs * 10 && atomic_load(&running); i++)
			usleep(100000);
		atomic_store(&running, 0);
		pthread_join(vt, NULL);
		pthread_join(ct, NULL);

		if (c.failed) {
			printf("%-8s DSA descriptor failed (check WQ config and IOMMU SVA)\n",
			       method_names[m]);
			continue;
		}
		double gbs = c.elapsed_ns ? (double)c.bytes / c.elapsed_ns : 0;
		double p50 = quantile(v.batches, v.nbatches, 0.50);
		double p99 = quantile(v.batches, v.nbatches, 0.99);
		double max = quantile(v.batches, v.nbatches, 1.0);
		printf("%-8s %14.1f %14.1f %14.1f %12.2f\n", method_names[m], p50, p99, max,
		       m == M_IDLE ? 0.0 : gbs);
		fflush(stdout);
	}
	printf("# victim cpu=%d ws=%zu MiB (%s)  copier cpu=%d buf=%zu MiB  %ds/method\n",
	       vcpu, lsize >> 20, vkind, ccpu, ssize >> 20, secs);
	if (have_dsa)
		dsa_close(&wq);
	return 0;
}
