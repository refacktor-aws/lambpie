#ifndef LAMBPIE_TLS_H
#define LAMBPIE_TLS_H

/*
 * Outbound TLS support for the lambpie C runtime.
 *
 * Dynamically links OpenSSL (libssl + libcrypto) at runtime using
 * dlopen/dlsym.  This gives zero binary cost: the shared libraries ship
 * with AL2023 and are loaded from the standard system paths at first use.
 *
 * The Lambda Runtime API itself (AWS_LAMBDA_RUNTIME_API on loopback) stays
 * plain HTTP — this API is only for outbound connections made by the
 * handler, e.g. calling AWS service endpoints or third-party HTTPS APIs.
 *
 * AL2023 library search order (tried in sequence until one opens):
 *   1. libssl.so.3    / libcrypto.so.3    (OpenSSL 3.x, default on AL2023)
 *   2. libssl.so.1.1  / libcrypto.so.1.1  (OpenSSL 1.1 compat layer)
 *
 * API:
 *   tls_global_init()             — call once at process start (idempotent)
 *   tls_connect(host, port)       — TCP+TLS handshake, returns opaque handle
 *   tls_send(conn, buf, len)      — write len bytes; returns bytes sent or -1
 *   tls_recv(conn, buf, len)      — read up to len bytes; returns count or -1
 *   tls_close(conn)               — shutdown TLS and close socket
 *
 * Error handling: all functions FATAL on unrecoverable errors (failed
 * dlopen, failed handshake, etc.).  tls_send/tls_recv return -1 only for
 * transient I/O conditions (EAGAIN/EINTR-equivalent from OpenSSL).
 *
 * Thread safety: not required.  Lambda processes one invocation at a time.
 */

#include <stddef.h>

/* Opaque TLS connection handle returned by tls_connect(). */
typedef struct tls_conn tls_conn;

/*
 * tls_global_init — load OpenSSL shared libraries and resolve symbols.
 *
 * Must be called before any other tls_* function.  Calling it more than
 * once is safe (idempotent after the first successful call).
 * FATALs if OpenSSL cannot be found on the system.
 */
void tls_global_init(void);

/*
 * tls_connect — open a TCP connection to host:port and complete TLS handshake.
 *
 * host    — null-terminated hostname (used for SNI and certificate validation)
 * port    — null-terminated decimal port string (e.g. "443")
 *
 * Returns a heap-allocated tls_conn on success.
 * FATALs on connection failure or TLS handshake failure.
 */
tls_conn *tls_connect(const char *host, const char *port);

/*
 * tls_send — write exactly len bytes to the TLS connection.
 *
 * Returns the number of bytes written (always len on success).
 * Returns -1 if the connection was closed or a fatal write error occurred
 * (in which case the connection should be considered broken and closed).
 * FATALs on unexpected OpenSSL errors.
 */
int tls_send(tls_conn *conn, const char *buf, int len);

/*
 * tls_recv — read up to len bytes from the TLS connection.
 *
 * Returns the number of bytes read (>= 1) on success.
 * Returns 0 if the connection was cleanly shut down by the peer.
 * Returns -1 on a transient read condition (retry is appropriate).
 * FATALs on unexpected OpenSSL errors.
 */
int tls_recv(tls_conn *conn, char *buf, int len);

/*
 * tls_close — send TLS close_notify and close the underlying TCP socket.
 *
 * After this call the conn pointer must not be used.
 */
void tls_close(tls_conn *conn);

#endif /* LAMBPIE_TLS_H */
