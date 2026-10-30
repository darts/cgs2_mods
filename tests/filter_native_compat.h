/* Native macOS tests consume the real Linux netfilter UAPI structs from Zig.
 * Only basic Linux typedefs and unrelated networking headers are substituted.
 * The production cross-build uses unmodified Linux headers without this file.
 */
#ifdef __APPLE__
#include <stdint.h>
#include <netinet/in.h>
#include <net/if.h>
#define _LINUX_TYPES_H
#define _LINUX_IF_H
#define _LINUX_IN_H
#define _LINUX_IN6_H
typedef uint8_t __u8;
typedef uint16_t __u16;
typedef uint32_t __u32;
typedef uint64_t __u64;
typedef int8_t __s8;
typedef int16_t __s16;
typedef int32_t __s32;
typedef int64_t __s64;
typedef __u16 __be16;
typedef __u32 __be32;
#endif
