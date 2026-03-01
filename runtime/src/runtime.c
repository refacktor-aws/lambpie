#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netdb.h>
#include <netinet/ip.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <sys/mman.h>
#include <errno.h>
#include <sys/time.h>
#include <stdio.h>
#include <stdlib.h>

#include "runtime.h"
#include "log.h"

/*
 * The Lambda Runtime API is a plain HTTP/1.1 service on the loopback address.
 * AWS_LAMBDA_RUNTIME_API is set to "host:port" (e.g. "127.0.0.1:9001") by the
 * Lambda execution environment.  We open a fresh TCP connection for every
 * request to avoid connection-state issues; the loopback RTT is negligible.
 *
 * Protocol reference:
 *   GET  /2018-06-01/runtime/invocation/next
 *   POST /2018-06-01/runtime/invocation/{request_id}/response
 *   POST /2018-06-01/runtime/invocation/{request_id}/error
 *   POST /2018-06-01/runtime/init/error
 */

struct runtime {
    http_recv_buffer *hb;
    struct addrinfo  *runtime_addrinfo;
    const char       *runtime_api;
    char             *response_buffer;
};

/* ---------------------------------------------------------------------------
 * Networking helpers
 * --------------------------------------------------------------------------- */

static inline struct addrinfo *resolve_host(const char *endpoint)
{
    LOG_VERBOSE_F("resolving endpoint: %s", endpoint);

    /* Split "host:port" — port is mandatory per the Lambda spec. */
    const char *colon = strchr(endpoint, ':');
    char host_no_port[256];
    const char *port;
    if (colon != NULL)
    {
        size_t host_len = (size_t)(colon - endpoint);
        FATAL(host_len >= sizeof(host_no_port),
              "Host portion of AWS_LAMBDA_RUNTIME_API is too long");
        memcpy(host_no_port, endpoint, host_len);
        host_no_port[host_len] = '\0';
        port = colon + 1;
    }
    else
    {
        /* Lambda always provides host:port; no port means misconfiguration. */
        size_t ep_len = strlen(endpoint);
        FATAL(ep_len >= sizeof(host_no_port),
              "AWS_LAMBDA_RUNTIME_API value is too long");
        memcpy(host_no_port, endpoint, ep_len + 1);
        port = "80";
        LOG_ERROR("AWS_LAMBDA_RUNTIME_API has no port — defaulting to 80 (likely misconfiguration)");
    }

    LOG_VERBOSE_F("resolved endpoint: host=[%s] port=[%s]", host_no_port, port);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_protocol = 0;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family   = AF_INET;
    hints.ai_flags    = AI_NUMERICSERV | AI_ADDRCONFIG;
    struct addrinfo *dns_result;

    int rc = getaddrinfo(host_no_port, port, &hints, &dns_result);
    FATAL(rc != 0, "getaddrinfo failed for AWS_LAMBDA_RUNTIME_API");
    (void)rc;
    return dns_result;
}

static int send_all(int sockfd, const char *buf, int len)
{
    int total_sent = 0;
    while (total_sent < len)
    {
        int rc = send(sockfd, buf + total_sent, len - total_sent, 0);
        FATAL(rc < 0, "send() failed");
        total_sent += rc;
    }
    return total_sent;
}

/*
 * socket_connect — create and connect a TCP socket.
 *
 * rcvtimeo_sec controls SO_RCVTIMEO:
 *   0  — no timeout (used for /next, which long-polls — may block for minutes)
 *  >0  — timeout in whole seconds (used for response/error POSTs)
 */
static int socket_connect(const struct addrinfo *addr, int rcvtimeo_sec)
{
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    FATAL(sockfd < 0, "socket() failed");

    if (rcvtimeo_sec > 0)
    {
        struct timeval timeout = {
            .tv_sec  = rcvtimeo_sec,
            .tv_usec = 0
        };
        int rc = setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO,
                            &timeout, sizeof(timeout));
        FATAL(rc < 0, "setsockopt(SO_RCVTIMEO) failed");
        (void)rc;
    }

    int rc = connect(sockfd, addr->ai_addr, addr->ai_addrlen);
    FATAL(rc < 0, "connect() to Lambda Runtime API failed");
    return sockfd;
}

/* ---------------------------------------------------------------------------
 * HTTP helper
 *
 * Sends one HTTP request and parses the response status + headers into hb.
 * rcvtimeo_sec is forwarded to socket_connect (0 = blocking).
 * recv_buf_size is the usable capacity of hb->buffer.data; it must be at
 * least as large as the response headers + body combined.  For normal
 * invocation traffic this is INCOMING_LAMBDA_REQUEST_BUFFER_SIZE (6 MB+).
 * For error POSTs (tiny response bodies) a small buffer is fine.
 * --------------------------------------------------------------------------- */

static void http(const runtime *rt, const char *path, const char *method,
                 const char *content, int req_content_length, int rcvtimeo_sec,
                 int recv_buf_size)
{
    const char *host         = rt->runtime_api;
    const struct addrinfo *addr = rt->runtime_addrinfo;
    http_recv_buffer *hb    = rt->hb;

    int sockfd = socket_connect(addr, rcvtimeo_sec);

    char request[MAX_HTTP_HEADER_SIZE];
    snprintf(request, sizeof(request),
             "%s %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
             "Content-Length: %d\r\n\r\n",
             method, path, host, req_content_length);

    send_all(sockfd, request, (int)strlen(request));

    if (req_content_length > 0)
    {
        send_all(sockfd, content, req_content_length);
    }

    char *response    = hb->buffer.data;
    char *parse_point = response;
    char *line_start  = response;
    char *body_start  = NULL;
    int   total_bytes_received = 0;
    int   content_length       = -1;
    int   remain               = recv_buf_size;
    char *delimiter            = NULL;
    hb->body.data        = NULL;
    hb->awsRequestId.data = NULL;

    while (remain)
    {
        FATAL(parse_point >= response + recv_buf_size,
              "HTTP response buffer overflow");

        if (parse_point >= response + total_bytes_received)
        {
            int bytes_received;
            do
            {
                bytes_received = recv(sockfd,
                                      response + total_bytes_received,
                                      (size_t)remain, 0);
            } while (bytes_received < 0 && errno == EINTR);

            FATAL(bytes_received <= 0,
                  "recv() returned 0 or error — connection closed by Lambda Runtime API");

            total_bytes_received += bytes_received;
            remain               -= bytes_received;
            if (body_start != NULL)
            {
                parse_point += bytes_received;
                continue;
            }
        }

        if (*parse_point == ':')
        {
            delimiter = parse_point;
        }
        else if (*parse_point == '\n')
        {
            FATAL(parse_point - response < 3,
                  "Unexpected line break in HTTP response before headers");
            FATAL(parse_point[-1] != '\r',
                  "Malformed HTTP line break: missing \\r before \\n");

            if (parse_point[-2] == '\n')
            {
                /* End of headers: blank line (\r\n\r\n). */
                FATAL(content_length < 0,
                      "HTTP response missing Content-Length header");
                body_start = parse_point + 1;
                remain     = content_length
                             - ((response + total_bytes_received) - body_start);
                parse_point = response + total_bytes_received;
                continue;
            }

            if (delimiter == NULL)
            {
                /* Status line (no ':' found). */
                if (total_bytes_received >= 12
                    && !memcmp(line_start, "HTTP/1.0 410", 12))
                {
                    /*
                     * 410: Lambda is shutting down the container.
                     * Exit cleanly; CloudWatch captures stderr up to this point.
                     */
                    LOG_INFO("Lambda container shutdown received (HTTP 410) — exiting");
                    close(sockfd);
                    exit(0);
                }
            }
            else if (!strncmp(line_start, "Content-Length:",
                              (size_t)(delimiter - line_start)))
            {
                content_length = atoi(delimiter + 2);
            }
            else if (!strncmp(line_start, "Lambda-Runtime-Aws-Request-Id:",
                              (size_t)(delimiter - line_start)))
            {
                hb->awsRequestId.data = delimiter + 2;
                hb->awsRequestId.len  =
                    (size_t)(parse_point - hb->awsRequestId.data) - 1; /* strip \r */
            }

            line_start = parse_point + 1;
            delimiter  = NULL;
        }

        ++parse_point;
    }

    response[total_bytes_received] = '\0';
    hb->buffer.len = (size_t)total_bytes_received;
    hb->body.data  = body_start;
    hb->body.len   = (size_t)(total_bytes_received - (body_start - response));
    close(sockfd);
}

/* ---------------------------------------------------------------------------
 * Large buffer allocation with guard page
 * --------------------------------------------------------------------------- */

static void *mapalloc(size_t size)
{
    int pagesize = getpagesize();
    /* Round up to page boundary then add one guard page at the end. */
    size = (size | (size_t)(pagesize - 1)) + 1 + (size_t)pagesize;

    void *ptr = mmap(NULL, size,
                     PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE,
                     -1, 0);
    FATAL(ptr == MAP_FAILED,
          "mmap() failed — cannot allocate runtime buffers");

    /* Guard page: PROT_NONE so overflows SIGSEGV loudly. */
    int rc = mprotect((char *)ptr + (size - (size_t)pagesize),
                      (size_t)pagesize, PROT_NONE);
    FATAL(rc != 0, "mprotect() failed — cannot install guard page");
    (void)rc;

    return ptr;
}

/* ---------------------------------------------------------------------------
 * Error endpoint helpers
 *
 * Lambda error JSON format (mandated by the Runtime API spec):
 *   {"errorMessage":"...","errorType":"...","stackTrace":[]}
 *
 * We serialise this by hand into a stack buffer (MAX_HTTP_HEADER_SIZE covers
 * it with ease) — no allocation, no json.c dependency here.
 * --------------------------------------------------------------------------- */

/*
 * write_error_json — serialise the Lambda error JSON into buf[0..bufsize).
 * Returns the number of bytes written (not including a NUL terminator).
 * FATALs if the buffer is too small (callers use a 4 KB stack buffer, which
 * is ample for any reasonable error_type + error_message pair).
 */
static int write_error_json(char *buf, int bufsize,
                            const char *error_type,
                            const char *error_message)
{
    int n = snprintf(buf, (size_t)bufsize,
                     "{\"errorMessage\":\"%s\","
                     "\"errorType\":\"%s\","
                     "\"stackTrace\":[]}",
                     error_message, error_type);
    FATAL(n < 0 || n >= bufsize,
          "write_error_json: error payload too large for internal buffer");
    return n;
}

/*
 * send_error_response — POST error JSON to the invocation error endpoint.
 *
 * Called after lambpie_handle() returns a failure code or otherwise
 * signals an error.  The Lambda service marks this invocation as failed
 * and routes the next event to the same execution environment.
 */
void send_error_response(const runtime *rt,
                         const char *error_type,
                         const char *error_message)
{
    FATAL(rt == NULL,
          "send_error_response called with NULL runtime — runtime_init() not called");
    FATAL(rt->hb->awsRequestId.data == NULL,
          "send_error_response called without a current request ID — "
          "call get_next_request() before send_error_response()");

    char path[256];
    snprintf(path, sizeof(path),
             "/2018-06-01/runtime/invocation/%.*s/error",
             (int)rt->hb->awsRequestId.len,
             rt->hb->awsRequestId.data);

    char body[4096];
    int body_len = write_error_json(body, (int)sizeof(body),
                                    error_type, error_message);

    LOG_ERROR_F("invocation error (type=%s): %s", error_type, error_message);

    /*
     * 30-second timeout — same as the response POST.
     * The ACK from Lambda is tiny; MAX_HTTP_HEADER_SIZE is more than enough.
     */
    http(rt, path, "POST", body, body_len, 30, MAX_HTTP_HEADER_SIZE);
}

/*
 * send_init_error — POST error JSON to the init error endpoint.
 *
 * Called when lambpie_init() fails, before the event loop starts.
 * Lambda marks the execution environment as failed.  The caller must
 * exit() after this call — there is no recovery path.
 *
 * This function constructs a temporary runtime so it can reuse the http()
 * helper.  The runtime_api env var must be set (as it always is inside
 * Lambda), otherwise we FATAL — which is still better than a silent hang.
 */
void send_init_error(const char *error_type,
                     const char *error_message)
{
    LOG_ERROR_F("init error (type=%s): %s", error_type, error_message);

    const char *runtime_api = getenv("AWS_LAMBDA_RUNTIME_API");
    if (runtime_api == NULL)
    {
        /* Can't report back — just die loudly.  The structured log line
         * above is already on stderr for CloudWatch. */
        LOG_FATAL("send_init_error: AWS_LAMBDA_RUNTIME_API not set — "
                  "cannot POST init error to Lambda service");
        /* LOG_FATAL calls exit(1); this line is unreachable. */
    }

    /*
     * Build a minimal throw-away runtime.  We don't call runtime_init()
     * here because:
     *   1. runtime_init() allocates 12 MB of mmapped buffers we don't need.
     *   2. It is idempotent-by-static but we may be on an error path before
     *      or instead of the normal init, so we avoid side effects.
     */
    struct addrinfo *addr = resolve_host(runtime_api);

    /* Small stack buffer for the request body and the response. */
    static char err_response_buffer[MAX_HTTP_HEADER_SIZE];
    static http_recv_buffer err_hb;
    err_hb.buffer.data = err_response_buffer;
    err_hb.buffer.len  = sizeof(err_response_buffer);
    err_hb.body.data   = NULL;
    err_hb.awsRequestId.data = NULL;

    static runtime err_rt;
    err_rt.hb                = &err_hb;
    err_rt.runtime_addrinfo  = addr;
    err_rt.runtime_api       = runtime_api;
    err_rt.response_buffer   = NULL;

    static const char *path = "/2018-06-01/runtime/init/error";

    char body[4096];
    int body_len = write_error_json(body, (int)sizeof(body),
                                    error_type, error_message);

    http(&err_rt, path, "POST", body, body_len, 30, MAX_HTTP_HEADER_SIZE);
    freeaddrinfo(addr);
}

/* ---------------------------------------------------------------------------
 * Public API
 * --------------------------------------------------------------------------- */

runtime *runtime_init(void)
{
    /* static: one runtime per process (Lambda is single-invocation). */
    static runtime rt;
    static http_recv_buffer hb;
    rt.hb = &hb;

    LOG_INFO("lambpie runtime init start");

    rt.runtime_api = getenv("AWS_LAMBDA_RUNTIME_API");
    FATAL(rt.runtime_api == NULL,
          "AWS_LAMBDA_RUNTIME_API environment variable is not set — "
          "this binary must run inside the Lambda execution environment");

    LOG_VERBOSE_F("runtime API endpoint: %s", rt.runtime_api);

    rt.runtime_addrinfo = resolve_host(rt.runtime_api);

    hb.buffer.data     = mapalloc(INCOMING_LAMBDA_REQUEST_BUFFER_SIZE);
    rt.response_buffer = mapalloc(OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE);

    LOG_INFO("lambpie runtime init complete");
    return &rt;
}

http_recv_buffer *get_next_request(const runtime *rt)
{
    static const char *path = "/2018-06-01/runtime/invocation/next";
    LOG_VERBOSE("polling for next invocation");
    /*
     * rcvtimeo_sec = 0: block indefinitely.
     * The /next endpoint long-polls; Lambda may hold the connection open for
     * many minutes on a warm-but-idle container.  Any finite timeout would
     * kill the container between invocations.
     */
    http(rt, path, "GET", "", 0, 0, INCOMING_LAMBDA_REQUEST_BUFFER_SIZE);
    FATAL(rt->hb->awsRequestId.data == NULL,
          "Lambda-Runtime-Aws-Request-Id header missing from /next response");
    LOG_INFO_F("invocation start: request_id=%.*s",
               (int)rt->hb->awsRequestId.len, rt->hb->awsRequestId.data);
    return rt->hb;
}

char *get_response_buffer(const runtime *rt)
{
    return rt->response_buffer;
}

void send_response(const runtime *rt, const char *response, size_t response_len)
{
    char path[256];
    snprintf(path, sizeof(path),
             "/2018-06-01/runtime/invocation/%.*s/response",
             (int)rt->hb->awsRequestId.len, rt->hb->awsRequestId.data);
    /*
     * rcvtimeo_sec = 30: give Lambda 30 seconds to ACK the response.
     * Lambda's maximum function timeout is 15 minutes, but the response POST
     * itself completes in well under a second on the loopback interface.
     */
    /*
     * The ACK response from Lambda is tiny; MAX_HTTP_HEADER_SIZE is enough.
     * We reuse hb->buffer.data (the 6 MB mmap) since it's already there, but
     * limit the parse to the header-sized portion.
     */
    http(rt, path, "POST", response, (int)response_len, 30, MAX_HTTP_HEADER_SIZE);
    LOG_INFO_F("invocation complete: request_id=%.*s bytes=%zu",
               (int)rt->hb->awsRequestId.len, rt->hb->awsRequestId.data,
               response_len);
}

void start_lambda(int (*handler)(const http_recv_buffer *, char *))
{
    runtime *rt = runtime_init();
    char *output_buffer = get_response_buffer(rt);

    while (1)
    {
        http_recv_buffer *hb = get_next_request(rt);
        int lambda_response_length = handler(hb, output_buffer);

        if (lambda_response_length < 0)
        {
            /*
             * Negative return: handler signals an error.
             * Report it to Lambda and continue the event loop —
             * the execution environment stays alive for the next invocation.
             */
            send_error_response(rt,
                                "Runtime.HandlerError",
                                "handler returned a negative response length");
        }
        else
        {
            send_response(rt, output_buffer, (size_t)lambda_response_length);
        }
    }
}
