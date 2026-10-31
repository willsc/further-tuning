/*
 * Cache-friendly copies for a logger that shares L3 with a hot thread.
 *
 *   nt_copy()     - prefetchnta loads + streaming stores. Works on Intel and AMD.
 *   dsa_memmove() - offloads the copy to an Intel DSA work queue. With
 *                   llc=0 the device writes to memory and leaves L3 alone;
 *                   llc=1 sets the cache-control flag and writes into L3.
 *
 * Header-only so the logger can just #include it.
 */
#ifndef FT_FASTCOPY_H
#define FT_FASTCOPY_H

#include <emmintrin.h>
#include <fcntl.h>
#include <linux/idxd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <xmmintrin.h>

#ifndef NT_PREFETCH_DIST
#define NT_PREFETCH_DIST 512
#endif

static inline void nt_copy(void *dst, const void *src, size_t len)
{
	char *d = dst;
	const char *s = src;

	/* Head: get dst 16-byte aligned for the streaming stores. */
	size_t head = (16 - ((uintptr_t)d & 15)) & 15;
	if (head > len)
		head = len;
	memcpy(d, s, head);
	d += head; s += head; len -= head;

	while (len >= 64) {
		_mm_prefetch(s + NT_PREFETCH_DIST, _MM_HINT_NTA);
		__m128i a = _mm_loadu_si128((const __m128i *)s);
		__m128i b = _mm_loadu_si128((const __m128i *)(s + 16));
		__m128i c = _mm_loadu_si128((const __m128i *)(s + 32));
		__m128i e = _mm_loadu_si128((const __m128i *)(s + 48));
		_mm_stream_si128((__m128i *)d, a);
		_mm_stream_si128((__m128i *)(d + 16), b);
		_mm_stream_si128((__m128i *)(d + 32), c);
		_mm_stream_si128((__m128i *)(d + 48), e);
		d += 64; s += 64; len -= 64;
	}
	memcpy(d, s, len);
	_mm_sfence();
}

struct dsa_wq {
	void *portal;
	int fd;
	int shared;		/* shared WQ: submit with ENQCMD, else MOVDIR64B */
	uint32_t max_xfer;
	struct dsa_hw_desc desc __attribute__((aligned(64)));
	struct dsa_completion_record comp __attribute__((aligned(32)));
};

static int dsa_sysfs_read(const char *wq, const char *attr, char *buf, size_t len)
{
	char path[256];
	snprintf(path, sizeof(path), "/sys/bus/dsa/devices/%s/%s", wq, attr);
	FILE *f = fopen(path, "r");
	if (!f)
		return -1;
	if (!fgets(buf, (int)len, f))
		buf[0] = 0;
	fclose(f);
	buf[strcspn(buf, "\n")] = 0;
	return 0;
}

/* path is a user WQ char device such as /dev/dsa/wq0.0. Returns 0 on success. */
static inline int dsa_open(struct dsa_wq *wq, const char *path)
{
	memset(wq, 0, sizeof(*wq));
	const char *name = strrchr(path, '/');
	name = name ? name + 1 : path;

	char buf[64];
	wq->shared = dsa_sysfs_read(name, "mode", buf, sizeof(buf)) == 0 &&
		     strcmp(buf, "shared") == 0;
	wq->max_xfer = 2U << 20;
	if (dsa_sysfs_read(name, "max_transfer_size", buf, sizeof(buf)) == 0)
		wq->max_xfer = (uint32_t)strtoul(buf, NULL, 0);

	wq->fd = open(path, O_RDWR);
	if (wq->fd < 0)
		return -1;
	wq->portal = mmap(NULL, 0x1000, PROT_WRITE, MAP_SHARED | MAP_POPULATE, wq->fd, 0);
	if (wq->portal == MAP_FAILED) {
		close(wq->fd);
		return -1;
	}
	return 0;
}

static inline void dsa_close(struct dsa_wq *wq)
{
	munmap(wq->portal, 0x1000);
	close(wq->fd);
}

static inline void dsa_movdir64b(void *portal, const void *desc)
{
	asm volatile(".byte 0x66, 0x0f, 0x38, 0xf8, 0x02"
		     : : "a"(portal), "d"(desc) : "memory");
}

/* Returns nonzero if the shared WQ was full and the submit must be retried. */
static inline int dsa_enqcmd(void *portal, const void *desc)
{
	uint8_t retry;
	asm volatile(".byte 0xf2, 0x0f, 0x38, 0xf8, 0x02\n\t"
		     "setz %0"
		     : "=r"(retry) : "a"(portal), "d"(desc) : "memory");
	return retry;
}

/* Submit one descriptor and spin until it completes. Returns the status. */
static inline int dsa_run(struct dsa_wq *wq, void *dst, const void *src,
			  uint32_t len, int llc)
{
	struct dsa_hw_desc *d = &wq->desc;
	memset(d, 0, sizeof(*d));
	d->opcode = DSA_OPCODE_MEMMOVE;
	d->flags = IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR | (llc ? IDXD_OP_FLAG_CC : 0);
	d->src_addr = (uintptr_t)src;
	d->dst_addr = (uintptr_t)dst;
	d->xfer_size = len;
	d->completion_addr = (uintptr_t)&wq->comp;
	wq->comp.status = 0;

	_mm_sfence();
	if (wq->shared) {
		while (dsa_enqcmd(wq->portal, d))
			_mm_pause();
	} else {
		dsa_movdir64b(wq->portal, d);
	}
	while (wq->comp.status == 0)
		_mm_pause();
	return wq->comp.status;
}

/*
 * Copy len bytes, splitting at the WQ's max transfer size and touching any
 * page the device faulted on before resuming. Returns 0 on success.
 */
static inline int dsa_memmove(struct dsa_wq *wq, void *dst, const void *src,
			      size_t len, int llc)
{
	char *d = dst;
	const char *s = src;
	while (len) {
		uint32_t n = len > wq->max_xfer ? wq->max_xfer : (uint32_t)len;
		int st = dsa_run(wq, d, s, n, llc);
		if (DSA_COMP_STATUS(st) == DSA_COMP_SUCCESS) {
			d += n; s += n; len -= n;
			continue;
		}
		if (DSA_COMP_STATUS(st) != DSA_COMP_PAGE_FAULT_NOBOF)
			return -1;
		volatile char *f = (volatile char *)(uintptr_t)wq->comp.fault_addr;
		if (st & DSA_COMP_STATUS_WRITE)
			*f = *f;
		else
			(void)*f;
		uint32_t done = wq->comp.bytes_completed;
		d += done; s += done; len -= done;
	}
	return 0;
}

#endif
