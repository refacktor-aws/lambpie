#ifndef LAMBPIE_LOG_H
#define LAMBPIE_LOG_H

/*
 * Structured JSON logger for the lambpie C runtime.
 *
 * All log output goes to stderr, which CloudWatch Logs captures verbatim.
 * Format: {"timestamp":"<epoch_ms>","level":"<LEVEL>","message":"<msg>"}
 *
 * Three levels:
 *   LOG_INFO  — lifecycle events always visible in production
 *   LOG_ERROR — recoverable error conditions, always visible
 *   LOG_FATAL — unrecoverable; logs then calls exit(1)
 *
 * Verbose events (per-invocation detail) are gated behind the
 * LAMBPIE_DEBUG environment variable.  Check lambpie_log_debug_enabled()
 * once at init and cache the result.
 *
 * Rules:
 *   - No dynamic allocation.
 *   - No external dependencies beyond libc.
 *   - The message string must not contain double-quotes; callers are
 *     responsible for sanitising or choosing literal messages.
 */

#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>  /* gettimeofday */

/* --------------------------------------------------------------------------
 * Internal: millisecond timestamp
 * -------------------------------------------------------------------------- */

static inline long long lambpie_log_now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (long long)tv.tv_sec * 1000LL + (long long)tv.tv_usec / 1000LL;
}

/* --------------------------------------------------------------------------
 * lambpie_log_debug_enabled — cached at first call
 * -------------------------------------------------------------------------- */

static inline int lambpie_log_debug_enabled(void)
{
    static int cached = -1;
    if (cached < 0) {
        const char *v = getenv("LAMBPIE_DEBUG");
        cached = (v != NULL && v[0] != '\0' && v[0] != '0') ? 1 : 0;
    }
    return cached;
}

/* --------------------------------------------------------------------------
 * Core emit macros
 *
 * LOG_INFO / LOG_ERROR write one line unconditionally.
 * LOG_VERBOSE writes only when LAMBPIE_DEBUG is set.
 * LOG_FATAL writes then exits — never returns.
 * -------------------------------------------------------------------------- */

#define LOG_INFO(msg) \
    fprintf(stderr, \
        "{\"timestamp\":\"%lld\",\"level\":\"INFO\",\"message\":\"%s\"}\n", \
        lambpie_log_now_ms(), (msg))

#define LOG_ERROR(msg) \
    fprintf(stderr, \
        "{\"timestamp\":\"%lld\",\"level\":\"ERROR\",\"message\":\"%s\"}\n", \
        lambpie_log_now_ms(), (msg))

#define LOG_FATAL(msg) \
    do { \
        fprintf(stderr, \
            "{\"timestamp\":\"%lld\",\"level\":\"FATAL\",\"message\":\"%s\"}\n", \
            lambpie_log_now_ms(), (msg)); \
        exit(1); \
    } while (0)

/* Verbose: compiled in always, but gated at runtime on LAMBPIE_DEBUG. */
#define LOG_VERBOSE(msg) \
    do { \
        if (lambpie_log_debug_enabled()) { \
            fprintf(stderr, \
                "{\"timestamp\":\"%lld\",\"level\":\"DEBUG\",\"message\":\"%s\"}\n", \
                lambpie_log_now_ms(), (msg)); \
        } \
    } while (0)

/* --------------------------------------------------------------------------
 * Formatted variants — for when you need a variable in the message.
 *
 * Usage:  LOG_INFO_F("invocation started: %s", request_id);
 *
 * These format into a stack buffer (512 bytes) then emit the structured
 * line.  Truncation is silent (the message is cut at 511 chars) because
 * a truncated log line is always better than a crash in the log path.
 * -------------------------------------------------------------------------- */

#define LOG_INFO_F(fmt, ...) \
    do { \
        char _lambpie_logbuf[512]; \
        snprintf(_lambpie_logbuf, sizeof(_lambpie_logbuf), fmt, __VA_ARGS__); \
        LOG_INFO(_lambpie_logbuf); \
    } while (0)

#define LOG_ERROR_F(fmt, ...) \
    do { \
        char _lambpie_logbuf[512]; \
        snprintf(_lambpie_logbuf, sizeof(_lambpie_logbuf), fmt, __VA_ARGS__); \
        LOG_ERROR(_lambpie_logbuf); \
    } while (0)

#define LOG_FATAL_F(fmt, ...) \
    do { \
        char _lambpie_logbuf[512]; \
        snprintf(_lambpie_logbuf, sizeof(_lambpie_logbuf), fmt, __VA_ARGS__); \
        LOG_FATAL(_lambpie_logbuf); \
    } while (0)

#define LOG_VERBOSE_F(fmt, ...) \
    do { \
        if (lambpie_log_debug_enabled()) { \
            char _lambpie_logbuf[512]; \
            snprintf(_lambpie_logbuf, sizeof(_lambpie_logbuf), fmt, __VA_ARGS__); \
            LOG_VERBOSE(_lambpie_logbuf); \
        } \
    } while (0)

#endif /* LAMBPIE_LOG_H */
