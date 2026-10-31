/*
 * wire2app - NIC-to-application latency per candidate core.
 *
 * Receive mode pins a busy-polling UDP receiver to each core in turn. For
 * every packet it takes three timestamps:
 *
 *   t0  NIC hardware RX stamp (PHC time, converted to CLOCK_REALTIME)
 *   t1  kernel software RX stamp
 *   t2  clock_gettime() right after recvmsg() returns
 *
 * and reports wire2app = t2 - t0, split into wire2kern (t1 - t0) and
 * kern2app (t2 - t1). The PHC-to-system offset is resampled 5 times a
 * second with PTP_SYS_OFFSET_EXTENDED, well under ENA's 125 reads/s limit.
 *
 * Send mode (-S) generates test traffic from another host.
 */
#include "common.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/errqueue.h>
#include <linux/ethtool.h>
#include <linux/net_tstamp.h>
#include <linux/ptp_clock.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdatomic.h>
#include <sys/ioctl.h>
#include <sys/socket.h>

#ifndef SO_PREFER_BUSY_POLL
#define SO_PREFER_BUSY_POLL 69
#endif

#define OFFSET_SAMPLES 5
#define OFFSET_PERIOD_US 200000

static _Atomic int64_t phc_offset_ns;	/* phc - realtime */
static atomic_int have_offset;
static atomic_int stop_offset;

struct offset_args {
	int fd;
	int cpu;
};

/* Best of OFFSET_SAMPLES readings: the one with the tightest system-time window. */
static int64_t ptp_ns(const struct ptp_clock_time *t)
{
	return t->sec * 1000000000LL + t->nsec;
}

/* Fallback for drivers without gettimex64: ts[] is sys, phc, sys, phc, ..., sys. */
static int sample_offset_basic(int fd, int64_t *off)
{
	struct ptp_sys_offset req = { .n_samples = OFFSET_SAMPLES };
	if (ioctl(fd, PTP_SYS_OFFSET, &req))
		return -1;
	int64_t best_win = INT64_MAX;
	for (unsigned i = 0; i < req.n_samples; i++) {
		int64_t s0 = ptp_ns(&req.ts[2 * i]);
		int64_t p = ptp_ns(&req.ts[2 * i + 1]);
		int64_t s1 = ptp_ns(&req.ts[2 * i + 2]);
		if (s1 - s0 < best_win) {
			best_win = s1 - s0;
			*off = p - (s0 + (s1 - s0) / 2);
		}
	}
	return 0;
}

static int sample_offset(int fd, int64_t *off)
{
	struct ptp_sys_offset_extended req = { .n_samples = OFFSET_SAMPLES };
	if (ioctl(fd, PTP_SYS_OFFSET_EXTENDED, &req))
		return sample_offset_basic(fd, off);
	int64_t best_win = INT64_MAX;
	for (unsigned i = 0; i < req.n_samples; i++) {
		struct ptp_clock_time *t = req.ts[i];
		int64_t s0 = ptp_ns(&t[0]);
		int64_t p = ptp_ns(&t[1]);
		int64_t s1 = ptp_ns(&t[2]);
		if (s1 - s0 < best_win) {
			best_win = s1 - s0;
			*off = p - (s0 + (s1 - s0) / 2);
		}
	}
	return 0;
}

static void *offset_thread(void *arg)
{
	struct offset_args *a = arg;
	if (a->cpu >= 0)
		pin_self(a->cpu);
	while (!atomic_load(&stop_offset)) {
		int64_t off;
		if (sample_offset(a->fd, &off) == 0) {
			atomic_store(&phc_offset_ns, off);
			atomic_store(&have_offset, 1);
		}
		usleep(OFFSET_PERIOD_US);
	}
	return NULL;
}

static int phc_index(int sock, const char *ifname)
{
	struct ethtool_ts_info info = { .cmd = ETHTOOL_GET_TS_INFO };
	struct ifreq ifr = { 0 };
	snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", ifname);
	ifr.ifr_data = (void *)&info;
	if (ioctl(sock, SIOCETHTOOL, &ifr))
		return -1;
	return info.phc_index;
}

static void enable_hw_rx_stamps(int sock, const char *ifname)
{
	struct hwtstamp_config cfg = { .tx_type = HWTSTAMP_TX_OFF, .rx_filter = HWTSTAMP_FILTER_ALL };
	struct ifreq ifr = { 0 };
	snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", ifname);
	ifr.ifr_data = (void *)&cfg;
	if (ioctl(sock, SIOCSHWTSTAMP, &ifr) == 0)
		return;
	int err = errno;
	memset(&cfg, 0, sizeof(cfg));
	if (ioctl(sock, SIOCGHWTSTAMP, &ifr) == 0 && cfg.rx_filter != HWTSTAMP_FILTER_NONE)
		return;
	fprintf(stderr, "warning: could not enable RX hardware timestamps on %s (%s); "
		"enable them with the driver's own method or no t0 will be reported\n",
		ifname, strerror(err));
}

static int open_rx_socket(const char *ifname, int port, const char *group, int busy_poll_us)
{
	int s = socket(AF_INET, SOCK_DGRAM, 0);
	if (s < 0)
		die("socket: %s", strerror(errno));
	int one = 1;
	setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(port) };
	if (bind(s, (struct sockaddr *)&addr, sizeof(addr)))
		die("bind port %d: %s", port, strerror(errno));
	if (ifname && setsockopt(s, SOL_SOCKET, SO_BINDTODEVICE, ifname, strlen(ifname)))
		fprintf(stderr, "warning: SO_BINDTODEVICE %s: %s\n", ifname, strerror(errno));
	if (group) {
		struct ip_mreqn mr = { .imr_ifindex = ifname ? (int)if_nametoindex(ifname) : 0 };
		inet_pton(AF_INET, group, &mr.imr_multiaddr);
		if (setsockopt(s, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mr, sizeof(mr)))
			die("join %s: %s", group, strerror(errno));
	}

	int flags = SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE |
		    SOF_TIMESTAMPING_RX_SOFTWARE | SOF_TIMESTAMPING_SOFTWARE;
	if (setsockopt(s, SOL_SOCKET, SO_TIMESTAMPING, &flags, sizeof(flags)))
		die("SO_TIMESTAMPING: %s", strerror(errno));

	if (busy_poll_us > 0) {
		setsockopt(s, SOL_SOCKET, SO_BUSY_POLL, &busy_poll_us, sizeof(busy_poll_us));
		setsockopt(s, SOL_SOCKET, SO_PREFER_BUSY_POLL, &one, sizeof(one));
	}
	return s;
}

struct core_stats {
	int cpu;
	size_t n, n_hw;
	double *w2a, *w2k, *k2a;
};

static void drain(int s)
{
	char buf[2048];
	while (recv(s, buf, sizeof(buf), MSG_DONTWAIT) > 0)
		;
}

static void run_core(int s, struct core_stats *st, size_t want, int timeout_s)
{
	char buf[2048], ctrl[512];
	struct iovec iov = { buf, sizeof(buf) };
	struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1,
			      .msg_control = ctrl };

	pin_self(st->cpu);
	drain(s);
	uint64_t deadline = now_ns() + (uint64_t)timeout_s * 1000000000ULL;

	while (st->n < want) {
		msg.msg_controllen = sizeof(ctrl);
		ssize_t r = recvmsg(s, &msg, MSG_DONTWAIT);
		if (r < 0) {
			if (now_ns() > deadline)
				break;
			continue;
		}
		struct timespec app;
		clock_gettime(CLOCK_REALTIME, &app);

		struct scm_timestamping *tss = NULL;
		for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c))
			if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SO_TIMESTAMPING)
				tss = (struct scm_timestamping *)CMSG_DATA(c);
		if (!tss)
			continue;

		int64_t t2 = ts_ns(&app);
		int64_t t1 = ts_ns(&tss->ts[0]);
		int64_t hw = ts_ns(&tss->ts[2]);
		size_t i = st->n++;
		st->k2a[i] = (double)(t2 - t1);
		if (hw && atomic_load(&have_offset)) {
			int64_t t0 = hw - atomic_load(&phc_offset_ns);
			st->w2a[st->n_hw] = (double)(t2 - t0);
			st->w2k[st->n_hw] = (double)(t1 - t0);
			st->n_hw++;
		}
	}
}

static int send_mode(const char *target, int rate, int size, int cpu)
{
	char host[256];
	snprintf(host, sizeof(host), "%s", target);
	char *colon = strrchr(host, ':');
	if (!colon)
		die("-S wants host:port");
	*colon = 0;

	struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_DGRAM }, *ai;
	if (getaddrinfo(host, colon + 1, &hints, &ai))
		die("cannot resolve %s", target);
	int s = socket(AF_INET, SOCK_DGRAM, 0);
	if (connect(s, ai->ai_addr, ai->ai_addrlen))
		die("connect: %s", strerror(errno));
	if (cpu >= 0)
		pin_self(cpu);

	char buf[9000] = { 0 };
	if (size > (int)sizeof(buf))
		size = sizeof(buf);
	uint64_t gap = 1000000000ULL / (rate > 0 ? rate : 1), next = now_ns();
	for (uint64_t seq = 0;; seq++) {
		while (now_ns() < next)
			cpu_relax();
		memcpy(buf, &seq, sizeof(seq));
		send(s, buf, size, 0);
		next += gap;
	}
}

static void usage(void)
{
	fprintf(stderr,
		"receive: wire2app -i IFACE -p PORT -c CPULIST [-g GROUP] [-n PKTS]\n"
		"                  [-T SEC] [-H HK_CPU] [-B BUSY_POLL_US] [-P /dev/ptpN]\n"
		"  -c  candidate hot cores, measured one at a time\n"
		"  -n  packets per core (default 20000)   -T  timeout per core (default 30)\n"
		"  -H  housekeeping CPU for the PHC offset sampler\n"
		"  -B  SO_BUSY_POLL microseconds (default 50, 0 disables)\n"
		"  -P  PHC device (default: from the interface's ethtool -T info)\n"
		"send:    wire2app -S HOST:PORT [-r PPS] [-z BYTES] [-C CPU]\n");
	exit(2);
}

int main(int argc, char **argv)
{
	const char *ifname = NULL, *group = NULL, *phc_path = NULL, *target = NULL;
	int port = 0, hk = -1, busy = 50, timeout = 30, rate = 10000, size = 64, scpu = -1;
	int cpus[CPU_SETSIZE], ncpus = 0, opt;
	size_t want = 20000;

	while ((opt = getopt(argc, argv, "i:p:c:g:n:T:H:B:P:S:r:z:C:h")) != -1) {
		switch (opt) {
		case 'i': ifname = optarg; break;
		case 'p': port = atoi(optarg); break;
		case 'c': ncpus = parse_cpulist(optarg, cpus, CPU_SETSIZE); break;
		case 'g': group = optarg; break;
		case 'n': want = strtoul(optarg, NULL, 0); break;
		case 'T': timeout = atoi(optarg); break;
		case 'H': hk = atoi(optarg); break;
		case 'B': busy = atoi(optarg); break;
		case 'P': phc_path = optarg; break;
		case 'S': target = optarg; break;
		case 'r': rate = atoi(optarg); break;
		case 'z': size = atoi(optarg); break;
		case 'C': scpu = atoi(optarg); break;
		default: usage();
		}
	}
	if (target)
		return send_mode(target, rate, size, scpu);
	if (!ifname || !port || !ncpus)
		usage();

	int s = open_rx_socket(ifname, port, group, busy);
	enable_hw_rx_stamps(s, ifname);

	char phc_buf[32];
	if (!phc_path) {
		int idx = phc_index(s, ifname);
		if (idx >= 0) {
			snprintf(phc_buf, sizeof(phc_buf), "/dev/ptp%d", idx);
			phc_path = phc_buf;
		}
	}
	pthread_t ot;
	struct offset_args oa = { .fd = -1, .cpu = hk };
	if (phc_path && (oa.fd = open(phc_path, O_RDONLY)) >= 0) {
		pthread_create(&ot, NULL, offset_thread, &oa);
		for (int i = 0; i < 20 && !atomic_load(&have_offset); i++)
			usleep(50000);
	}
	if (!atomic_load(&have_offset))
		fprintf(stderr, "warning: no PHC offset (%s); only kern2app is reported. "
			"On ENA, enable the PHC with the enable_phc devlink parameter.\n",
			phc_path ? phc_path : "no PHC on interface");

	struct core_stats *st = calloc(ncpus, sizeof(*st));
	for (int i = 0; i < ncpus; i++) {
		st[i].cpu = cpus[i];
		st[i].w2a = malloc(want * sizeof(double));
		st[i].w2k = malloc(want * sizeof(double));
		st[i].k2a = malloc(want * sizeof(double));
		fprintf(stderr, "measuring cpu %d...\n", cpus[i]);
		run_core(s, &st[i], want, timeout);
	}
	atomic_store(&stop_offset, 1);

	printf("%5s %8s %12s %12s %13s %12s %14s %13s\n", "cpu", "pkts",
	       "w2a_p50_ns", "w2a_p99_ns", "w2a_p999_ns", "w2a_max_ns",
	       "wire2kern_p50", "kern2app_p50");
	for (int i = 0; i < ncpus; i++) {
		struct core_stats *c = &st[i];
		double k2a = quantile(c->k2a, c->n, 0.5);
		if (c->n_hw) {
			double w2k = quantile(c->w2k, c->n_hw, 0.5);
			double p50 = quantile(c->w2a, c->n_hw, 0.5);
			double p99 = quantile(c->w2a, c->n_hw, 0.99);
			double p999 = quantile(c->w2a, c->n_hw, 0.999);
			double max = quantile(c->w2a, c->n_hw, 1.0);
			printf("%5d %8zu %12.0f %12.0f %13.0f %12.0f %14.0f %13.0f\n", c->cpu,
			       c->n, p50, p99, p999, max, w2k, k2a);
		} else {
			printf("%5d %8zu %12s %12s %13s %12s %14s %13.0f\n", c->cpu, c->n,
			       "-", "-", "-", "-", "-", k2a);
		}
	}
	printf("# iface=%s port=%d phc=%s busy_poll=%dus\n", ifname, port,
	       atomic_load(&have_offset) ? phc_path : "none", busy);
	if (oa.fd >= 0)
		pthread_join(ot, NULL);
	return 0;
}
