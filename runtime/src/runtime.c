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

#include "runtime.h"

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
    struct addrinfo *runtime_addrinfo;
    const char *runtime_api;
    char *response_buffer;
};

/* ---------------------------------------------------------------------------
 * Networking helpers
 * --------------------------------------------------------------------------- */

static inline struct addrinfo *resolve_host(const char *endpoint)
{
    DEBUG("Resolving endpoint: %s\n", endpoint);

    /* Split "host:port" — port is mandatory per the Lambda spec. */
    const char *colon = strchr(endpoint, ':');
    char host_no_port[256];
    const char *port;
    if (colon != NULL)
    {
        size_t host_len = (size_t)(colon - endpoint);
        FATAL(host_len >= sizeof(host_no_port), "Host portion of AWS_LAMBDA_RUNTIME_API is too long");
        memcpy(host_no_port, endpoint, host_len);
        host_no_port[host_len] = '\0';
        port = colon + 1;
    }
    else
    {
        /* Lambda always provides host:port, but fall back to port 80 rather
         * than silently proceeding with a garbage address. */
        size_t ep_len = strlen(endpoint);
        FATAL(ep_len >= sizeof(host_no_port), "AWS_LAMBDA_RUNTIME_API value is too long");
        memcpy(host_no_port, endpoint, ep_len + 1);
        port = "80";
        LOG("WARNING: AWS_LAMBDA_RUNTIME_API has no port, defaulting to 80\n");
    }
    DEBUG("Parsed endpoint into host=[%s] and port=[%s]\n", host_no_port, port);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_protocol = 0;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family   = AF_INET;
    hints.ai_flags    = AI_NUMERICSERV | AI_ADDRCONFIG;
    struct addrinfo *dns_result;

    int rc = getaddrinfo(host_no_port, port, &hints, &dns_result);
    DEBUG("getaddrinfo[%s:%s] returned rc=%d\n", host_no_port, port, rc);
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
        DEBUG("Sent %d bytes, total sent=%d\n", rc, total_sent);
    }
    return total_sent;
}

/*
 * socket_connect — create and connect a TCP socket.
 *
 * rcvtimeo_sec controls SO_RCVTIMEO:
 *   0  → no timeout (used for /next — Lambda blocks this until an event arrives,
 *         which can be minutes on a warm container)
 *  >0  → timeout in whole seconds (used for /response POST)
 *
 * A 1-second receive timeout on /next would kill every warm invocation that
 * idles longer than 1 second, which is the common case.
 */
static int socket_connect(const struct addrinfo *addr, int rcvtimeo_sec)
{
    DEBUG("Creating socket\n");
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    FATAL(sockfd < 0, "socket() failed");

    if (rcvtimeo_sec > 0)
    {
        DEBUG("Setting SO_RCVTIMEO to %d second(s)\n", rcvtimeo_sec);
        struct timeval timeout = {
            .tv_sec  = rcvtimeo_sec,
            .tv_usec = 0
        };
        int rc = setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        FATAL(rc < 0, "setsockopt(SO_RCVTIMEO) failed");
        (void)rc;
    }

    DEBUG("Connecting socket\n");
    int rc = connect(sockfd, addr->ai_addr, addr->ai_addrlen);
    FATAL(rc < 0, "connect() to Lambda Runtime API failed");
    DEBUG("Connected\n");
    return sockfd;
}

/* ---------------------------------------------------------------------------
 * HTTP helper
 *
 * Sends one HTTP request and parses the response into hb.
 * rcvtimeo_sec is forwarded to socket_connect (0 = blocking).
 * --------------------------------------------------------------------------- */

static void http(const runtime *rt, const char *path, const char *method,
                 const char *content, int req_content_length, int rcvtimeo_sec)
{
    const char *host = rt->runtime_api;
    const struct addrinfo *addr = rt->runtime_addrinfo;
    http_recv_buffer *hb = rt->hb;

    DEBUG("Making HTTP request to host=[%s], path=[%s], method=[%s]\n", host, path, method);
    int sockfd = socket_connect(addr, rcvtimeo_sec);

    char request[MAX_HTTP_HEADER_SIZE];
    snprintf(request, sizeof(request),
             "%s %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
             "Content-Length: %d\r\n\r\n",
             method, path, host, req_content_length);

    DEBUG("Request headers:\n<<<\n%s\n>>>\n", request);
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
    int   remain               = INCOMING_LAMBDA_REQUEST_BUFFER_SIZE;
    char *delimiter            = NULL;
    hb->body.data        = NULL;
    hb->awsRequestId.data = NULL;

    while (remain)
    {
        FATAL(parse_point >= response + INCOMING_LAMBDA_REQUEST_BUFFER_SIZE,
              "HTTP response buffer overflow");

        if (parse_point >= response + total_bytes_received)
        {
            DEBUG("Calling recv with ptr=%d, remain=%d\n", total_bytes_received, remain);
            int bytes_received;
            do
            {
                bytes_received = recv(sockfd, response + total_bytes_received, (size_t)remain, 0);
            } while (bytes_received < 0 && errno == EINTR);

            DEBUG("Received %d bytes\n", bytes_received);
            FATAL(bytes_received <= 0, "recv() returned 0 or error — connection closed by Lambda Runtime API");

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
            FATAL(parse_point - response < 3, "Unexpected line break in HTTP response before headers");
            FATAL(parse_point[-1] != '\r', "Malformed HTTP line break: missing \\r before \\n");

            if (parse_point[-2] == '\n')
            {
                /* End of headers: blank line (\r\n\r\n seen as \n...\n). */
                FATAL(content_length < 0, "HTTP response missing Content-Length header");
                body_start = parse_point + 1;
                remain     = content_length - ((response + total_bytes_received) - body_start);
                DEBUG("BODY START: %p, remain=%d\n", body_start, remain);
                parse_point = response + total_bytes_received;
                continue;
            }

            if (delimiter == NULL)
            {
                /* Status line (no ':' found on this line). */
                if (total_bytes_received >= 12 && !memcmp(line_start, "HTTP/1.0 410", 12))
                {
                    /* 410 means Lambda is shutting down the container.
                     * Exit cleanly — CloudWatch will capture anything written
                     * to stderr before this point. */
                    LOG("Lambda container shutdown (HTTP 410)\n");
                    close(sockfd);
                    exit(0);
                }
            }
            else if (!strncmp(line_start, "Content-Length:",
                              (size_t)(delimiter - line_start)))
            {
                content_length = atoi(delimiter + 2);
                DEBUG("HEADER Content-Length: %d\n", content_length);
            }
            else if (!strncmp(line_start, "Lambda-Runtime-Aws-Request-Id:",
                              (size_t)(delimiter - line_start)))
            {
                hb->awsRequestId.data = delimiter + 2;
                hb->awsRequestId.len  = (size_t)(parse_point - hb->awsRequestId.data) - 1; /* strip \r */
                DEBUG("HEADER Lambda-Runtime-Aws-Request-Id: [%.*s]\n",
                      (int)hb->awsRequestId.len, hb->awsRequestId.data);
            }

            line_start = parse_point + 1;
            delimiter  = NULL;
        }

        ++parse_point;
    }

    DEBUG("Total bytes received: %d\n", total_bytes_received);
    response[total_bytes_received] = '\0';
    hb->buffer.len = (size_t)total_bytes_received;
    hb->body.data  = body_start;
    hb->body.len   = (size_t)(total_bytes_received - (body_start - response));
    close(sockfd);
    DEBUG("Response received\n");
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
    FATAL(ptr == MAP_FAILED, "mmap() failed — cannot allocate runtime buffers");

    /* Install a PROT_NONE guard page at the end so overflows SIGSEGV loudly. */
    int rc = mprotect((char *)ptr + (size - (size_t)pagesize), (size_t)pagesize, PROT_NONE);
    FATAL(rc != 0, "mprotect() failed — cannot install guard page");
    (void)rc;

    DEBUG("Allocated %zu bytes at %p (guard page at %p)\n",
          size, ptr, (char *)ptr + (size - (size_t)pagesize));
    return ptr;
}

/* ---------------------------------------------------------------------------
 * Public API
 * --------------------------------------------------------------------------- */

runtime *runtime_init(void)
{
    /* static: one runtime per process. Lambda processes one invocation at a time. */
    static runtime rt;
    static http_recv_buffer hb;
    rt.hb = &hb;

    rt.runtime_api = getenv("AWS_LAMBDA_RUNTIME_API");
    FATAL(rt.runtime_api == NULL,
          "AWS_LAMBDA_RUNTIME_API environment variable is not set — "
          "this binary must run inside the Lambda execution environment");
    DEBUG("Runtime API: %s\n", rt.runtime_api);

    rt.runtime_addrinfo = resolve_host(rt.runtime_api);

    hb.buffer.data      = mapalloc(INCOMING_LAMBDA_REQUEST_BUFFER_SIZE);
    rt.response_buffer  = mapalloc(OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE);
    return &rt;
}

http_recv_buffer *get_next_request(const runtime *rt)
{
    static const char *path = "/2018-06-01/runtime/invocation/next";
    DEBUG("Polling for next invocation\n");
    /*
     * rcvtimeo_sec = 0: block indefinitely.
     * The /next endpoint long-polls; Lambda may hold the connection open for
     * many minutes on a warm-but-idle container.  Any finite timeout here
     * would kill the container between invocations.
     */
    http(rt, path, "GET", "", 0, 0 /* no timeout */);
    FATAL(rt->hb->awsRequestId.data == NULL,
          "Lambda-Runtime-Aws-Request-Id header missing from /next response");
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
     * itself should complete in well under a second on the loopback interface.
     */
    http(rt, path, "POST", response, (int)response_len, 30 /* seconds */);
}

void start_lambda(int (*handler)(const http_recv_buffer *, char *))
{
    runtime *rt = runtime_init();
    char *output_buffer = get_response_buffer(rt);

    while (1)
    {
        http_recv_buffer *hb = get_next_request(rt);
        int lambda_response_length = handler(hb, output_buffer);
        send_response(rt, output_buffer, (size_t)lambda_response_length);
    }
}
