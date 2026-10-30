/* SPDX-License-Identifier: MIT
 * CGS2 IPv4 egress policy, using the kernel's existing iptables mangle hooks.
 * No DNS, shell execution, daemon, connection tracking, or implicit LAN access.
 * IPv6 must be disabled separately before bringing up any network interface.
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <linux/netfilter_ipv4/ip_tables.h>

#define MAX_ALLOWS 64
#define MAX_TABLE_BYTES (1024U * 1024U)
#define ALL_HOOKS ((1U << NF_INET_NUMHOOKS) - 1)
#define MARKER_V1 "CGS2_PRIVACY_V1"
#define MARKER_V2 "CGS2_PRIVACY_V2"
#define MARKER "CGS2_PRIVACY_V3"
#define STD_SIZE (sizeof(struct ipt_entry) + XT_ALIGN(sizeof(struct xt_standard_target)))
#define MATCH_SIZE XT_ALIGN(sizeof(struct xt_entry_match) + sizeof(struct xt_tcp))
#define RULE_SIZE (STD_SIZE + MATCH_SIZE)
#define ERROR_SIZE (sizeof(struct ipt_entry) + XT_ALIGN(sizeof(struct xt_error_target)))

/* The device uses the native 64-bit Linux UAPI, not the compat ABI. */
_Static_assert(sizeof(void *) == 8, "64-bit userspace required");
_Static_assert(sizeof(struct ipt_entry) == 112, "unexpected ipt_entry ABI");
_Static_assert(sizeof(struct ipt_replace) == 96, "unexpected ipt_replace ABI");
_Static_assert(sizeof(struct ipt_get_entries) == 40, "unexpected get_entries ABI");
_Static_assert(MATCH_SIZE == XT_ALIGN(sizeof(struct xt_entry_match) + sizeof(struct xt_udp)),
               "TCP/UDP match layout mismatch");

struct allow {
    struct in_addr address;
    uint16_t port;
    uint16_t protocol;
};
struct policy {
    struct allow allows[MAX_ALLOWS];
    size_t count;
};

static const char *const hook_names[] = {
    "PREROUTING", "INPUT", "FORWARD", "OUTPUT", "POSTROUTING"
};

static void die(const char *message)
{
    fprintf(stderr, "cgs2-filter: %s\n", message);
    exit(EXIT_FAILURE);
}

static void die_errno(const char *message)
{
    fprintf(stderr, "cgs2-filter: %s: %s\n", message, strerror(errno));
    exit(EXIT_FAILURE);
}

static void *allocate(size_t bytes)
{
    void *p = calloc(1, bytes);
    if (!p) die_errno("allocate");
    return p;
}

static bool parse_allow(const char *text, struct allow *out)
{
    char address[INET_ADDRSTRLEN], canonical[INET_ADDRSTRLEN];
    const char *start, *colon, *p;
    unsigned int port = 0;
    memset(out, 0, sizeof(*out));
    if (strncmp(text, "tcp:", 4) == 0) out->protocol = IPPROTO_TCP;
    else if (strncmp(text, "udp:", 4) == 0) out->protocol = IPPROTO_UDP;
    else return false;
    start = text + 4;
    colon = strchr(start, ':');
    if (!colon || colon == start || (size_t)(colon - start) >= sizeof(address)) return false;
    memcpy(address, start, (size_t)(colon - start));
    address[colon - start] = '\0';
    if (inet_pton(AF_INET, address, &out->address) != 1) return false;
    if (!inet_ntop(AF_INET, &out->address, canonical, sizeof(canonical)) ||
        strcmp(address, canonical)) return false;
    /* 0.0.0.0 is not a meaningful remote permission. */
    if (out->address.s_addr == INADDR_ANY) return false;
    p = colon + 1;
    if (*p < '1' || *p > '9') return false;
    for (; *p; p++) {
        if (*p < '0' || *p > '9') return false;
        port = port * 10U + (unsigned int)(*p - '0');
        if (port > 65535U) return false;
    }
    out->port = (uint16_t)port;
    return true;
}

static bool add_allow(struct policy *policy, const char *text)
{
    struct allow entry;
    if (!parse_allow(text, &entry)) return false;
    for (size_t i = 0; i < policy->count; i++) {
        if (memcmp(&entry, &policy->allows[i], sizeof(entry)) == 0) return true;
    }
    if (policy->count == MAX_ALLOWS) return false;
    policy->allows[policy->count++] = entry;
    return true;
}

static void read_config(struct policy *policy, const char *path)
{
    FILE *file = fopen(path, "r");
    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;
    unsigned int number = 0;
    if (!file) die_errno("open allowlist");
    while ((length = getline(&line, &capacity, file)) >= 0) {
        char *start = line, *end;
        number++;
        if (length > 255 || memchr(line, '\0', (size_t)length))
            die("allowlist line too long or contains a NUL byte");
        while (*start == ' ' || *start == '\t') start++;
        end = start + strlen(start);
        while (end > start && (end[-1] == '\n' || end[-1] == '\r' ||
                              end[-1] == ' ' || end[-1] == '\t')) *--end = '\0';
        if (!*start || *start == '#') continue;
        if (!add_allow(policy, start)) {
            fprintf(stderr, "cgs2-filter: invalid allowlist line %u (or more than %d entries)\n",
                    number, MAX_ALLOWS);
            exit(EXIT_FAILURE);
        }
    }
    free(line);
    if (ferror(file)) die_errno("read allowlist");
    if (fclose(file)) die_errno("close allowlist");
}

static struct ipt_entry *at(struct ipt_replace *table, unsigned int offset)
{
    return (struct ipt_entry *)((unsigned char *)table->entries + offset);
}

static struct ipt_entry *append_standard(struct ipt_replace *table, int verdict,
                                         const struct allow *allow)
{
    struct ipt_entry *entry = at(table, table->size);
    struct xt_standard_target *target;
    entry->target_offset = sizeof(*entry);
    if (allow) {
        struct xt_entry_match *match = (struct xt_entry_match *)entry->elems;
        entry->ip.dst = allow->address;
        entry->ip.dmsk.s_addr = UINT32_MAX;
        entry->ip.proto = allow->protocol;
        match->u.user.match_size = MATCH_SIZE;
        strcpy(match->u.user.name, allow->protocol == IPPROTO_TCP ? "tcp" : "udp");
        if (allow->protocol == IPPROTO_TCP) {
            struct xt_tcp *tcp = (struct xt_tcp *)match->data;
            tcp->spts[1] = UINT16_MAX;
            tcp->dpts[0] = tcp->dpts[1] = allow->port;
        } else {
            struct xt_udp *udp = (struct xt_udp *)match->data;
            udp->spts[1] = UINT16_MAX;
            udp->dpts[0] = udp->dpts[1] = allow->port;
        }
        entry->target_offset += MATCH_SIZE;
    }
    entry->next_offset = entry->target_offset + XT_ALIGN(sizeof(*target));
    target = (struct xt_standard_target *)((unsigned char *)entry + entry->target_offset);
    target->target.u.user.target_size = XT_ALIGN(sizeof(*target));
    target->verdict = -verdict - 1;
    table->size += entry->next_offset;
    table->num_entries++;
    return entry;
}

static struct ipt_replace *build_table_version(const struct policy *policy, bool stock,
                                              unsigned int version)
{
    const size_t capacity = sizeof(struct ipt_replace) +
        (NF_INET_NUMHOOKS + 1) * STD_SIZE + (MAX_ALLOWS + 1) * RULE_SIZE + ERROR_SIZE;
    struct ipt_replace *table = allocate(capacity);
    strcpy(table->name, "mangle");
    table->valid_hooks = ALL_HOOKS;
    for (unsigned int hook = 0; hook < NF_INET_NUMHOOKS; hook++) {
        table->hook_entry[hook] = table->size;
        if (!stock && version >= 2 && hook == NF_INET_LOCAL_IN) {
            /* Reject network ADB even if a peer selects an allowed broker
             * port as its source. Preserve local TCP proxying on exact 'lo'. */
            const struct allow adb = {.port = 5555, .protocol = IPPROTO_TCP};
            struct ipt_entry *blocked = append_standard(table, NF_DROP, &adb);
            blocked->ip.dmsk.s_addr = 0; /* Any local destination address. */
            if (version >= 3) {
                memcpy(blocked->ip.iniface, "lo", 3);
                memset(blocked->ip.iniface_mask, 0xff, 3);
                blocked->ip.invflags = IPT_INV_VIA_IN;
            }
        }
        if (!stock && hook == NF_INET_LOCAL_OUT) {
            struct ipt_entry *loopback = append_standard(table, NF_ACCEPT, NULL);
            memcpy(loopback->ip.outiface, "lo", 3);
            /* Include the NUL: match 'lo' exactly, not a 'lo*' prefix. */
            memset(loopback->ip.outiface_mask, 0xff, 3);
            for (size_t i = 0; i < policy->count; i++)
                append_standard(table, NF_ACCEPT, &policy->allows[i]);
        }
        table->underflow[hook] = table->size;
        append_standard(table, !stock && (hook == NF_INET_LOCAL_OUT || hook == NF_INET_FORWARD)
                                      ? NF_DROP : NF_ACCEPT, NULL);
    }
    /* The ERROR sentinel identifies our canonical table, not an executable chain. */
    struct ipt_entry *entry = at(table, table->size);
    struct xt_error_target *target = (struct xt_error_target *)entry->elems;
    entry->target_offset = sizeof(*entry);
    entry->next_offset = ERROR_SIZE;
    target->target.u.user.target_size = XT_ALIGN(sizeof(*target));
    strcpy(target->target.u.user.name, XT_ERROR_TARGET);
    strcpy(target->errorname, stock ? "ERROR" : version == 1 ? MARKER_V1 :
                             version == 2 ? MARKER_V2 : MARKER);
    table->size += entry->next_offset;
    table->num_entries++;
    return table;
}

static struct ipt_replace *build_table(const struct policy *policy, bool stock)
{
    return build_table_version(policy, stock, 3);
}

/* Validate framing before examining a table returned by the kernel. */
static bool valid_entries(const struct ipt_getinfo *info, const void *bytes)
{
    unsigned int offset = 0, count = 0;
    if (info->valid_hooks != ALL_HOOKS || info->size > MAX_TABLE_BYTES) return false;
    while (offset < info->size) {
        const struct ipt_entry *entry = (const struct ipt_entry *)((const char *)bytes + offset);
        if (info->size - offset < sizeof(*entry) ||
            entry->next_offset < sizeof(*entry) + sizeof(struct xt_entry_target) ||
            entry->next_offset > info->size - offset ||
            XT_ALIGN(entry->next_offset) != entry->next_offset ||
            entry->target_offset < sizeof(*entry) ||
            entry->target_offset > entry->next_offset - sizeof(struct xt_entry_target)) return false;
        const struct xt_entry_target *target = (const struct xt_entry_target *)
            ((const char *)entry + entry->target_offset);
        if (target->u.user.target_size != entry->next_offset - entry->target_offset) return false;
        unsigned int match_offset = sizeof(*entry);
        while (match_offset < entry->target_offset) {
            const struct xt_entry_match *match = (const struct xt_entry_match *)
                ((const char *)entry + match_offset);
            if (entry->target_offset - match_offset < sizeof(*match) ||
                match->u.match_size < sizeof(*match) ||
                match->u.match_size > entry->target_offset - match_offset ||
                XT_ALIGN(match->u.match_size) != match->u.match_size) return false;
            match_offset += match->u.match_size;
        }
        offset += entry->next_offset;
        count++;
    }
    return offset == info->size && count == info->num_entries;
}

static bool same_table(const struct ipt_getinfo *info, const void *bytes,
                       const struct ipt_replace *expected)
{
    if (!valid_entries(info, bytes) || info->num_entries != expected->num_entries ||
        info->size != expected->size ||
        memcmp(info->hook_entry, expected->hook_entry, sizeof(info->hook_entry)) ||
        memcmp(info->underflow, expected->underflow, sizeof(info->underflow))) return false;
    void *normalized = allocate(info->size);
    memcpy(normalized, bytes, info->size);
    for (unsigned int off = 0; off < info->size;) {
        struct ipt_entry *entry = (struct ipt_entry *)((char *)normalized + off);
        memset(&entry->counters, 0, sizeof(entry->counters));
        entry->comefrom = 0;  /* Kernel hook traversal bookkeeping. */
        off += entry->next_offset;
    }
    bool same = memcmp(normalized, expected->entries, info->size) == 0;
    free(normalized);
    return same;
}

static bool recognized_table(const struct ipt_getinfo *info, const void *bytes)
{
    struct policy existing = {0};
    struct ipt_replace *expected = build_table(&existing, true);
    bool stock = same_table(info, bytes, expected);
    free(expected);
    if (stock) return true;
    if (!valid_entries(info, bytes)) return false;
    /* Extract candidate permissions, then compare every normalized byte against
     * a reconstruction. A marker alone is insufficient proof of ownership. */
    for (unsigned int offset = 0; offset < info->size;) {
        const struct ipt_entry *entry = (const struct ipt_entry *)((const char *)bytes + offset);
        if (offset >= info->hook_entry[NF_INET_LOCAL_OUT] &&
            offset < info->underflow[NF_INET_LOCAL_OUT] &&
            entry->target_offset == sizeof(*entry) + MATCH_SIZE &&
            (entry->ip.proto == IPPROTO_TCP || entry->ip.proto == IPPROTO_UDP)) {
            const struct xt_entry_match *match = (const struct xt_entry_match *)entry->elems;
            if (existing.count == MAX_ALLOWS) return false;
            struct allow *allow = &existing.allows[existing.count++];
            allow->protocol = entry->ip.proto;
            allow->address = entry->ip.dst;
            if (allow->protocol == IPPROTO_TCP)
                allow->port = ((const struct xt_tcp *)match->data)->dpts[0];
            else allow->port = ((const struct xt_udp *)match->data)->dpts[0];
        }
        offset += entry->next_offset;
    }
    expected = build_table(&existing, false);
    bool ours = same_table(info, bytes, expected);
    free(expected);
    for (unsigned int version = 2; !ours && version >= 1; version--) {
        /* Permit an atomic upgrade only from an exact previous canonical
         * policy, never from a marker-only or partially modified table. */
        expected = build_table_version(&existing, false, version);
        ours = same_table(info, bytes, expected);
        free(expected);
    }
    return ours;
}

static void dump_table(const struct ipt_getinfo *info, const void *bytes)
{
    if (!valid_entries(info, bytes)) die("invalid kernel table layout");
    printf("table=mangle owner=%s entries=%u\n", recognized_table(info, bytes) ? "recognized" : "unknown",
           info->num_entries);
    for (unsigned int offset = 0; offset < info->size;) {
        const struct ipt_entry *entry = (const struct ipt_entry *)((const char *)bytes + offset);
        const struct xt_entry_target *target = (const struct xt_entry_target *)
            ((const char *)entry + entry->target_offset);
        for (unsigned int hook = 0; hook < NF_INET_NUMHOOKS; hook++)
            if (info->hook_entry[hook] == offset) printf("%s:\n", hook_names[hook]);
        printf("  offset=%u packets=%" PRIu64 " bytes=%" PRIu64,
               offset, (uint64_t)entry->counters.pcnt, (uint64_t)entry->counters.bcnt);
        if (!target->u.user.name[0] && target->u.user.target_size == XT_ALIGN(sizeof(struct xt_standard_target))) {
            int verdict = ((const struct xt_standard_target *)target)->verdict;
            printf(" verdict=%s", verdict == -NF_ACCEPT - 1 ? "ACCEPT" :
                                     verdict == -NF_DROP - 1 ? "DROP" : "OTHER");
        } else printf(" target=%.29s", target->u.user.name);
        if (entry->ip.outiface[0]) printf(" out=%.16s", entry->ip.outiface);
        if (entry->ip.iniface[0])
            printf(" in%s%.16s", entry->ip.invflags & IPT_INV_VIA_IN ? "!=" : "=",
                   entry->ip.iniface);
        if (entry->ip.proto) printf(" protocol=%u", entry->ip.proto);
        if (entry->ip.dmsk.s_addr) {
            char address[INET_ADDRSTRLEN];
            if (!inet_ntop(AF_INET, &entry->ip.dst, address, sizeof(address))) die_errno("inet_ntop");
            printf(" destination=%s", address);
        }
        if (entry->target_offset == sizeof(*entry) + MATCH_SIZE) {
            const struct xt_entry_match *match = (const struct xt_entry_match *)entry->elems;
            if (!strcmp(match->u.user.name, "tcp"))
                printf(" port=%u", ((const struct xt_tcp *)match->data)->dpts[0]);
            if (!strcmp(match->u.user.name, "udp"))
                printf(" port=%u", ((const struct xt_udp *)match->data)->dpts[0]);
        }
        putchar('\n');
        offset += entry->next_offset;
    }
}

static void info_from_table(struct ipt_getinfo *info, const struct ipt_replace *table)
{
    memset(info, 0, sizeof(*info));
    strcpy(info->name, table->name);
    info->valid_hooks = table->valid_hooks;
    memcpy(info->hook_entry, table->hook_entry, sizeof(info->hook_entry));
    memcpy(info->underflow, table->underflow, sizeof(info->underflow));
    info->num_entries = table->num_entries;
    info->size = table->size;
}

#ifndef CGS2_FILTER_TEST
static struct ipt_get_entries *read_table(int fd, struct ipt_getinfo *info)
{
    memset(info, 0, sizeof(*info));
    strcpy(info->name, "mangle");
    socklen_t length = sizeof(*info);
    if (getsockopt(fd, IPPROTO_IP, IPT_SO_GET_INFO, info, &length) != 0) die_errno("read mangle info");
    if (length != sizeof(*info) || info->size > MAX_TABLE_BYTES) die("unexpected mangle table size");
    length = (socklen_t)(sizeof(struct ipt_get_entries) + info->size);
    struct ipt_get_entries *entries = allocate(length);
    strcpy(entries->name, "mangle");
    entries->size = info->size;
    if (getsockopt(fd, IPPROTO_IP, IPT_SO_GET_ENTRIES, entries, &length) != 0) die_errno("read mangle entries");
    if (length != sizeof(*entries) + info->size || !valid_entries(info, entries->entrytable))
        die("unexpected mangle table layout");
    return entries;
}

static void usage(void)
{
    fputs("Usage: cgs2-filter status\n"
          "       cgs2-filter {apply|dry-run} [--config FILE] [--allow tcp:IPv4:port|udp:IPv4:port]...\n"
          "IPv4 only; disable IPv6 separately. Empty egress policy allows only loopback.\n", stderr);
    exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
    struct policy policy = {0};
    bool status, dry_run;
    if (argc < 2) usage();
    status = strcmp(argv[1], "status") == 0;
    dry_run = strcmp(argv[1], "dry-run") == 0;
    if (!status && !dry_run && strcmp(argv[1], "apply")) usage();
    if (status && argc != 2) usage();
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) usage();
        if (!strcmp(argv[i], "--config")) read_config(&policy, argv[i + 1]);
        else if (!strcmp(argv[i], "--allow")) {
            if (!add_allow(&policy, argv[i + 1])) die("invalid --allow (or too many entries)");
        } else usage();
    }
    struct ipt_replace *replacement = build_table(&policy, false);
    struct ipt_getinfo info;
    if (dry_run) {
        info_from_table(&info, replacement);
        dump_table(&info, replacement->entries);
        free(replacement);
        return EXIT_SUCCESS;
    }
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) die_errno("socket");
    struct ipt_get_entries *existing = read_table(fd, &info);
    if (status) dump_table(&info, existing->entrytable);
    else {
        if (!recognized_table(&info, existing->entrytable))
            die("refusing to overwrite an unknown mangle policy");
        replacement->num_counters = info.num_entries;
        replacement->counters = allocate(info.num_entries * sizeof(struct xt_counters));
        socklen_t length = (socklen_t)(sizeof(*replacement) + replacement->size);
        /* A single kernel transaction, with no flush/ACCEPT interval. */
        if (setsockopt(fd, IPPROTO_IP, IPT_SO_SET_REPLACE, replacement, length) != 0)
            die_errno("atomically install mangle policy");
        free(existing);
        existing = read_table(fd, &info);
        if (!same_table(&info, existing->entrytable, replacement))
            die("installed table differs from requested policy");
        printf("Installed IPv4 default-deny policy with %zu explicit endpoint(s).\n", policy.count);
        free(replacement->counters);
    }
    free(existing);
    free(replacement);
    if (close(fd) != 0) die_errno("close");
    return EXIT_SUCCESS;
}
#endif
