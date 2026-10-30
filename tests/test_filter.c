#define CGS2_FILTER_TEST
#include "filter_native_compat.h"
#include "../src/cgs2-filter.c"
#include <assert.h>
#include <sys/wait.h>

static int verdict(const struct ipt_entry *entry)
{
    const struct xt_standard_target *target = (const struct xt_standard_target *)
        ((const char *)entry + entry->target_offset);
    return -target->verdict - 1;
}

/* Minimal independent packet evaluator for the exact rule subset this tool
 * emits. Exercise security properties against the serialized kernel payload. */
static int evaluate_packet(struct ipt_replace *table, unsigned int hook,
                    const char *in_interface, const char *out_interface, const char *destination,
                    uint16_t protocol, uint16_t source_port, uint16_t port)
{
    struct in_addr dst;
    assert(inet_pton(AF_INET, destination, &dst) == 1);
    char iface[IFNAMSIZ] = {0}, input[IFNAMSIZ] = {0};
    assert(strlen(out_interface) < sizeof(iface));
    assert(strlen(in_interface) < sizeof(input));
    strcpy(iface, out_interface);
    strcpy(input, in_interface);
    for (unsigned int offset = table->hook_entry[hook];; ) {
        const struct ipt_entry *entry = at(table, offset);
        bool matches = true;
        bool input_matches = true;
        for (size_t i = 0; i < sizeof(iface); i++)
            if (((unsigned char)iface[i] ^ (unsigned char)entry->ip.outiface[i]) &
                entry->ip.outiface_mask[i]) matches = false;
        for (size_t i = 0; i < sizeof(input); i++)
            if (((unsigned char)input[i] ^ (unsigned char)entry->ip.iniface[i]) &
                entry->ip.iniface_mask[i]) input_matches = false;
        if (entry->ip.invflags & IPT_INV_VIA_IN) input_matches = !input_matches;
        if (!input_matches) matches = false;
        if ((dst.s_addr & entry->ip.dmsk.s_addr) != entry->ip.dst.s_addr) matches = false;
        if (entry->ip.proto && entry->ip.proto != protocol) matches = false;
        if (entry->target_offset > sizeof(*entry)) {
            const struct xt_entry_match *match = (const struct xt_entry_match *)entry->elems;
            const uint16_t *ports = (const uint16_t *)match->data;
            if (source_port < ports[0] || source_port > ports[1]) matches = false;
            if (port < ports[2] || port > ports[3]) matches = false;
        }
        if (matches) return verdict(entry);
        offset += entry->next_offset;
        assert(offset <= table->underflow[hook]);
    }
}

static int evaluate(struct ipt_replace *table, unsigned int hook,
                    const char *out_interface, const char *destination,
                    uint16_t protocol, uint16_t port)
{
    return evaluate_packet(table, hook, "wlan0", out_interface, destination, protocol, 0, port);
}

static void test_parser(void)
{
    struct allow allow;
    const char *bad[] = {
        "", "tcp", "TCP:192.168.1.2:1883", "tcp:hostname:1883", "icmp:192.168.1.2:1",
        "tcp:192.168.1.2:0", "tcp:192.168.1.2:65536", "tcp:192.168.1.2:999999999999999999",
        "tcp:192.168.1.2:-1", "tcp:192.168.1.2:+1", "tcp:192.168.1.2:01", "tcp:192.168.1.2:1x",
        "tcp:192.168.1.2: 1", "tcp:192.168.1.2:1 ", "tcp:192.168.1.2:1\n",
        "tcp:192.168.1.2:", "tcp:192.168.1.2:1:2", "tcp:192.168.1.256:1",
        "tcp:192.168.1.2/24:1", "tcp:127.1:1", "tcp:0xc0a80102:1",
        "tcp:0.0.0.0:1", "tcp:[::1]:1", "tcp:192.168.1.02:1"
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        if (parse_allow(bad[i], &allow)) fprintf(stderr, "incorrectly accepted: %s\n", bad[i]);
        assert(!parse_allow(bad[i], &allow));
    }
    assert(parse_allow("tcp:192.168.1.2:1883", &allow));
    assert(allow.protocol == IPPROTO_TCP && allow.port == 1883);
    assert(parse_allow("udp:255.255.255.255:67", &allow));
    assert(parse_allow("udp:192.168.1.2:65535", &allow));
    struct policy policy = {0};
    assert(add_allow(&policy, "tcp:192.168.1.2:1883"));
    assert(add_allow(&policy, "tcp:192.168.1.2:1883"));
    assert(policy.count == 1);
}

static void test_default_deny(void)
{
    struct policy empty = {0};
    struct ipt_replace *table = build_table(&empty, false);
    struct ipt_getinfo info;
    info_from_table(&info, table);
    assert(table->num_entries == 8);
    assert(table->valid_hooks == 31);
    assert(recognized_table(&info, table->entries));
    for (unsigned int hook = 0; hook < NF_INET_NUMHOOKS; hook++) {
        assert(info.hook_entry[hook] <= info.underflow[hook]);
        assert(verdict(at(table, info.underflow[hook])) ==
               ((hook == NF_INET_LOCAL_OUT || hook == NF_INET_FORWARD) ? NF_DROP : NF_ACCEPT));
    }
    assert(evaluate(table, NF_INET_LOCAL_OUT, "lo", "127.0.0.1", IPPROTO_TCP, 1234) == NF_ACCEPT);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "loevil", "127.0.0.1", IPPROTO_TCP, 1234) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.2", IPPROTO_TCP, 1883) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "8.8.8.8", IPPROTO_UDP, 53) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "1.1.1.1", IPPROTO_ICMP, 0) == NF_DROP);
    assert(evaluate(table, NF_INET_FORWARD, "lo", "127.0.0.1", IPPROTO_TCP, 1234) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_IN, "", "192.168.1.10", IPPROTO_TCP, 5555) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_IN, "", "127.0.0.1", IPPROTO_TCP, 5037) == NF_ACCEPT);
    assert(evaluate(table, NF_INET_LOCAL_IN, "", "192.168.1.10", IPPROTO_TCP, 8080) == NF_ACCEPT);
    assert(evaluate(table, NF_INET_LOCAL_IN, "", "192.168.1.10", IPPROTO_UDP, 5555) == NF_ACCEPT);
    assert(evaluate_packet(table, NF_INET_LOCAL_IN, "lo", "", "127.0.0.1",
                           IPPROTO_TCP, 1883, 5555) == NF_ACCEPT);
    assert(evaluate_packet(table, NF_INET_LOCAL_IN, "loevil", "", "127.0.0.1",
                           IPPROTO_TCP, 1883, 5555) == NF_DROP);
    /* Destination/source addresses cannot impersonate the ingress interface. */
    assert(evaluate_packet(table, NF_INET_LOCAL_IN, "wlan0", "", "127.0.0.1",
                           IPPROTO_TCP, 1883, 5555) == NF_DROP);
    free(table);
}

static void test_explicit_permissions(void)
{
    struct policy policy = {0};
    assert(add_allow(&policy, "tcp:192.168.1.2:1883"));
    assert(add_allow(&policy, "udp:192.168.1.3:123"));
    struct ipt_replace *table = build_table(&policy, false);
    struct ipt_getinfo info;
    info_from_table(&info, table);
    assert(recognized_table(&info, table->entries));
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.2", IPPROTO_TCP, 1883) == NF_ACCEPT);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.2", IPPROTO_UDP, 1883) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.2", IPPROTO_TCP, 1884) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.4", IPPROTO_TCP, 1883) == NF_DROP);
    assert(evaluate(table, NF_INET_LOCAL_OUT, "wlan0", "192.168.1.3", IPPROTO_UDP, 123) == NF_ACCEPT);
    assert(evaluate(table, NF_INET_FORWARD, "wlan0", "192.168.1.2", IPPROTO_TCP, 1883) == NF_DROP);
    /* A permitted broker endpoint must not make ADB reachable by choosing
     * source port 1883: its return traffic would otherwise match OUTPUT. */
    assert(evaluate_packet(table, NF_INET_LOCAL_IN, "wlan0", "", "192.168.1.10",
                           IPPROTO_TCP, 1883, 5555) == NF_DROP);
    assert(evaluate_packet(table, NF_INET_LOCAL_OUT, "", "wlan0", "192.168.1.2",
                           IPPROTO_TCP, 5555, 1883) == NF_ACCEPT);
    /* Counters and kernel hook bookkeeping do not invalidate recognition. */
    struct ipt_entry *entry = at(table, table->hook_entry[NF_INET_LOCAL_OUT]);
    entry->counters.pcnt = 999;
    entry->counters.bcnt = 4096;
    entry->comefrom = 8;
    assert(recognized_table(&info, table->entries));
    /* An extra broad accept must never be mistaken for our canonical policy. */
    memset(entry->ip.outiface_mask, 0, sizeof(entry->ip.outiface_mask));
    assert(!recognized_table(&info, table->entries));
    entry->next_offset = 0;
    assert(!valid_entries(&info, table->entries));
    free(table);
}

static void test_stock_and_capacity(void)
{
    struct policy policy = {0};
    struct ipt_replace *table = build_table(&policy, true);
    struct ipt_getinfo info;
    info_from_table(&info, table);
    assert(recognized_table(&info, table->entries));
    info.underflow[NF_INET_LOCAL_OUT]++;
    assert(!recognized_table(&info, table->entries));
    free(table);
    for (int i = 1; i <= MAX_ALLOWS; i++) {
        char text[64];
        snprintf(text, sizeof(text), "tcp:192.168.1.2:%d", i);
        assert(add_allow(&policy, text));
    }
    assert(!add_allow(&policy, "tcp:192.168.1.2:65535"));
    table = build_table(&policy, false);
    info_from_table(&info, table);
    assert(table->num_entries == MAX_ALLOWS + 8);
    assert(recognized_table(&info, table->entries));
    free(table);
}

static void test_legacy_upgrade(void)
{
    struct policy policy = {0};
    for (unsigned int endpoints = 0; endpoints < 2; endpoints++) {
        if (endpoints) assert(add_allow(&policy, "tcp:192.168.1.2:1883"));
        for (unsigned int version = 1; version <= 2; version++) {
            struct ipt_replace *legacy = build_table_version(&policy, false, version);
            struct ipt_getinfo info;
            info_from_table(&info, legacy);
            assert(legacy->num_entries == 6 + version + endpoints);
            assert(recognized_table(&info, legacy->entries));
            struct ipt_replace *current = build_table(&policy, false);
            assert(!same_table(&info, legacy->entries, current));
            free(current);
            struct ipt_entry *entry = at(legacy, legacy->hook_entry[NF_INET_LOCAL_OUT]);
            memset(entry->ip.outiface_mask, 0, sizeof(entry->ip.outiface_mask));
            assert(!recognized_table(&info, legacy->entries));
            free(legacy);
        }
    }
}

static void config_fixture(const void *bytes, size_t length, bool success, size_t count)
{
    char path[] = "/tmp/cgs2-filter-test-XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    assert(write(fd, bytes, length) == (ssize_t)length);
    assert(close(fd) == 0);
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        struct policy policy = {0};
        if (!success) assert(freopen("/dev/null", "w", stderr));
        read_config(&policy, path);
        assert(policy.count == count);
        _exit(0);
    }
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status));
    assert((WEXITSTATUS(status) == 0) == success);
    assert(unlink(path) == 0);
}

static void test_config(void)
{
    const char *valid = "# explicitly authorized endpoints\r\n\n  tcp:192.168.1.2:1883 \r\n"
                        "udp:192.168.1.3:123\n\ttcp:192.168.1.2:1883";
    config_fixture(valid, strlen(valid), true, 2);
    config_fixture("", 0, true, 0);
    const char nul[] = "tcp:192.168.1.2:1883\0invisible junk\n";
    config_fixture(nul, sizeof(nul) - 1, false, 0);
    const char *invalid = "tcp:192.168.1.2:1883\nudp:invalid:53\n";
    config_fixture(invalid, strlen(invalid), false, 0);
    invalid = "tcp:192.168.1.2:1883 # inline comments forbidden\n";
    config_fixture(invalid, strlen(invalid), false, 0);
    char oversized[300];
    memset(oversized, ' ', sizeof(oversized));
    config_fixture(oversized, sizeof(oversized), false, 0);
}

int main(void)
{
    test_parser();
    test_default_deny();
    test_explicit_permissions();
    test_stock_and_capacity();
    test_legacy_upgrade();
    test_config();
    puts("filter tests: parser, config, serialized rule behavior, ownership, framing, capacity passed");
    return 0;
}
