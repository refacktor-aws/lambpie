#ifndef LAMBPIE_RUNTIME_H
#define LAMBPIE_RUNTIME_H

#include <stddef.h>   /* size_t */
#include <stdio.h>    /* fprintf */
#include <stdlib.h>   /* exit   */

/* ---------------------------------------------------------------------------
 * Legacy macros — kept for backward compatibility with call sites in
 * runtime.c that predate the structured logger.
 *
 * New code should use log.h macros directly.
 * --------------------------------------------------------------------------- */

/* LOG always writes to stderr — visible in CloudWatch Logs. */
#define LOG(...) fprintf(stderr, __VA_ARGS__)

/* FATAL must be active in ALL builds. A silent no-op in production would allow
 * corrupted state to continue executing instead of dying immediately.
 * Never gate this on RELEASE. */
#define FATAL(COND, MSG) do { if (COND) { fprintf(stderr, "FATAL: " MSG "\n"); exit(1); } } while (0)

/* DEBUG is compiled out in release builds. Gate behind LAMBPIE_DEBUG so it
 * does not conflict with system-level NDEBUG. */
#ifdef LAMBPIE_DEBUG
#define DEBUG(...) fprintf(stderr, __VA_ARGS__)
#else
#define DEBUG(...) ((void)0)
#endif

/* ---------------------------------------------------------------------------
 * Buffer sizes
 * --------------------------------------------------------------------------- */

#define MAX_REQUEST_SIZE 6 * 1048576
#define MAX_RESPONSE_SIZE 6 * 1048576
#define MAX_HTTP_HEADER_SIZE 8 * 1024
#define INCOMING_LAMBDA_REQUEST_BUFFER_SIZE (MAX_REQUEST_SIZE + MAX_HTTP_HEADER_SIZE)
#define OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE (MAX_RESPONSE_SIZE)

/* ---------------------------------------------------------------------------
 * Core types
 * --------------------------------------------------------------------------- */

typedef struct {
    char *data;
    size_t len;
} slice;

typedef struct {
    slice buffer;
    slice awsRequestId;
    slice body;
} http_recv_buffer;

/* ---------------------------------------------------------------------------
 * High-level interface ("bring your own handler")
 * --------------------------------------------------------------------------- */

void start_lambda(int (*handler)(const http_recv_buffer*, char *));

/* ---------------------------------------------------------------------------
 * Low-level interface ("bring your own loop") — simplifies Rust FFI
 * --------------------------------------------------------------------------- */

typedef struct runtime runtime;

runtime* runtime_init(void);

char *get_response_buffer(const runtime *rt);

http_recv_buffer* get_next_request(const runtime *rt);

void send_response(const runtime *rt, const char *response_buffer, size_t response_len);

/* ---------------------------------------------------------------------------
 * Error reporting — Lambda Runtime API error endpoints
 *
 * send_error_response: POST /2018-06-01/runtime/invocation/{request_id}/error
 *   Call when lambpie_handle() returns a non-zero status or crashes.
 *   The runtime then continues to the next invocation.
 *
 * send_init_error: POST /2018-06-01/runtime/init/error
 *   Call when lambpie_init() fails before the event loop starts.
 *   Lambda will mark the execution environment as failed and not route
 *   further invocations to it.  The caller must exit() after this call.
 *
 * error_type   — machine-readable type string, e.g. "Runtime.HandlerError"
 * error_message — human-readable description
 *
 * Both functions post the canonical Lambda error JSON:
 *   {"errorMessage":"...","errorType":"...","stackTrace":[]}
 * --------------------------------------------------------------------------- */

void send_error_response(const runtime *rt,
                         const char *error_type,
                         const char *error_message);

void send_init_error(const char *error_type,
                     const char *error_message);

#endif /* LAMBPIE_RUNTIME_H */
