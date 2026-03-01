#ifndef LAMBPIE_RUNTIME_H
#define LAMBPIE_RUNTIME_H

#include <stddef.h>   /* size_t */
#include <stdio.h>    /* fprintf */
#include <stdlib.h>   /* exit   */

/* LOG always writes to stderr — visible in CloudWatch Logs in both debug and release. */
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

#define MAX_REQUEST_SIZE 6 * 1048576
#define MAX_RESPONSE_SIZE 6 * 1048576
#define MAX_HTTP_HEADER_SIZE 8 * 1024
#define INCOMING_LAMBDA_REQUEST_BUFFER_SIZE (MAX_REQUEST_SIZE + MAX_HTTP_HEADER_SIZE)
#define OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE (MAX_RESPONSE_SIZE)

typedef struct {
    char *data;
    size_t len;
} slice;

typedef struct {
    slice buffer;
    slice awsRequestId;
    slice body;
} http_recv_buffer;

// higher-level interface ("bring your own handler"):

void start_lambda(int (*handler)(const http_recv_buffer*, char *));

// lower-level interface ("bring your own loop") - simplifies access via Rust FFI:

typedef struct runtime runtime;

runtime* runtime_init();

char *get_response_buffer(const runtime *rt);

http_recv_buffer* get_next_request(const runtime *rt);

void send_response(const runtime *rt, const char *response_buffer, size_t response_len);

#endif /* LAMBPIE_RUNTIME_H */
