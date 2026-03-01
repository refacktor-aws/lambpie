#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include "tls.h"
#include "log.h"
#include "runtime.h"   /* FATAL macro */

#include <dlfcn.h>
#include <string.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netdb.h>
#include <unistd.h>
#include <errno.h>

/*
 * ---------------------------------------------------------------------------
 * OpenSSL opaque type forwards (we never include <openssl/*.h>)
 *
 * These match the ABI of both OpenSSL 1.1.x and 3.x — the structs are
 * opaque in those versions so we only ever hold pointers to them.
 * ---------------------------------------------------------------------------
 */
typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_st     SSL;
typedef struct ssl_method_st SSL_METHOD;

/* ---------------------------------------------------------------------------
 * Function pointer table (populated by tls_global_init)
 * --------------------------------------------------------------------------- */

static struct {
    /* libcrypto */
    void (*OPENSSL_init_crypto)(unsigned long long opts, const void *settings);

    /* libssl */
    const SSL_METHOD *(*TLS_client_method)(void);
    SSL_CTX          *(*SSL_CTX_new)(const SSL_METHOD *method);
    void              (*SSL_CTX_free)(SSL_CTX *ctx);
    long              (*SSL_CTX_set_options)(SSL_CTX *ctx, long options);
    int               (*SSL_CTX_set_default_verify_paths)(SSL_CTX *ctx);
    SSL              *(*SSL_new)(SSL_CTX *ctx);
    void              (*SSL_free)(SSL *ssl);
    int               (*SSL_set_fd)(SSL *ssl, int fd);
    int               (*SSL_set_tlsext_host_name)(SSL *ssl, const char *name);
    int               (*SSL_connect)(SSL *ssl);
    int               (*SSL_write)(SSL *ssl, const void *buf, int num);
    int               (*SSL_read)(SSL *ssl, void *buf, int num);
    int               (*SSL_shutdown)(SSL *ssl);
    int               (*SSL_get_error)(const SSL *ssl, int ret);
    unsigned long     (*ERR_get_error)(void);
    void              (*ERR_error_string_n)(unsigned long e, char *buf, size_t len);
} ssl_fn;

static int tls_initialized = 0;

/* ---------------------------------------------------------------------------
 * Internal: resolve one symbol from a dlopen handle, FATAL if missing.
 * --------------------------------------------------------------------------- */

static void *require_sym(void *handle, const char *name)
{
    void *sym = dlsym(handle, name);
    FATAL(sym == NULL, name);   /* message is the symbol name — concise and useful */
    return sym;
}

/* ---------------------------------------------------------------------------
 * tls_global_init
 * --------------------------------------------------------------------------- */

void tls_global_init(void)
{
    if (tls_initialized) {
        return;
    }

    /*
     * Try OpenSSL 3.x first (AL2023 default), then 1.1 as a fallback.
     * RTLD_GLOBAL so that libssl can find libcrypto symbols.
     */
    static const char *const crypto_candidates[] = {
        "libcrypto.so.3",
        "libcrypto.so.1.1",
        NULL
    };
    static const char *const ssl_candidates[] = {
        "libssl.so.3",
        "libssl.so.1.1",
        NULL
    };

    void *h_crypto = NULL;
    for (int i = 0; crypto_candidates[i] != NULL; i++) {
        h_crypto = dlopen(crypto_candidates[i], RTLD_NOW | RTLD_GLOBAL);
        if (h_crypto != NULL) {
            LOG_VERBOSE_F("tls_global_init: loaded %s", crypto_candidates[i]);
            break;
        }
    }
    FATAL(h_crypto == NULL, "tls_global_init: could not dlopen libcrypto — "
          "OpenSSL is not installed (expected libcrypto.so.3 or libcrypto.so.1.1 on AL2023)");

    void *h_ssl = NULL;
    for (int i = 0; ssl_candidates[i] != NULL; i++) {
        h_ssl = dlopen(ssl_candidates[i], RTLD_NOW | RTLD_GLOBAL);
        if (h_ssl != NULL) {
            LOG_VERBOSE_F("tls_global_init: loaded %s", ssl_candidates[i]);
            break;
        }
    }
    FATAL(h_ssl == NULL, "tls_global_init: could not dlopen libssl — "
          "OpenSSL is not installed (expected libssl.so.3 or libssl.so.1.1 on AL2023)");

    /*
     * Resolve all required symbols.  FATAL on any missing symbol so we
     * crash loudly at init rather than segfaulting mid-request.
     *
     * OPENSSL_init_crypto was introduced in OpenSSL 1.1; on 1.0.x the
     * library self-initialises.  We treat its absence as non-fatal.
     */
    ssl_fn.OPENSSL_init_crypto     = dlsym(h_crypto, "OPENSSL_init_crypto");
    /* non-fatal: may be NULL on very old builds — library auto-inits */

    ssl_fn.TLS_client_method             = require_sym(h_ssl, "TLS_client_method");
    ssl_fn.SSL_CTX_new                   = require_sym(h_ssl, "SSL_CTX_new");
    ssl_fn.SSL_CTX_free                  = require_sym(h_ssl, "SSL_CTX_free");
    ssl_fn.SSL_CTX_set_options           = require_sym(h_ssl, "SSL_CTX_set_options");
    ssl_fn.SSL_CTX_set_default_verify_paths = require_sym(h_ssl, "SSL_CTX_set_default_verify_paths");
    ssl_fn.SSL_new                       = require_sym(h_ssl, "SSL_new");
    ssl_fn.SSL_free                      = require_sym(h_ssl, "SSL_free");
    ssl_fn.SSL_set_fd                    = require_sym(h_ssl, "SSL_set_fd");
    ssl_fn.SSL_connect                   = require_sym(h_ssl, "SSL_connect");
    ssl_fn.SSL_write                     = require_sym(h_ssl, "SSL_write");
    ssl_fn.SSL_read                      = require_sym(h_ssl, "SSL_read");
    ssl_fn.SSL_shutdown                  = require_sym(h_ssl, "SSL_shutdown");
    ssl_fn.SSL_get_error                 = require_sym(h_ssl, "SSL_get_error");
    ssl_fn.ERR_get_error                 = require_sym(h_crypto, "ERR_get_error");
    ssl_fn.ERR_error_string_n            = require_sym(h_crypto, "ERR_error_string_n");

    /*
     * SSL_set_tlsext_host_name is a macro in the headers but resolves as a
     * function call to SSL_ctrl in the library.  We bind SSL_ctrl directly
     * and call it with the correct arguments in tls_connect.
     */
    ssl_fn.SSL_set_tlsext_host_name = dlsym(h_ssl, "SSL_set_tlsext_host_name");
    /* acceptable if NULL — SNI just won't be sent, cert validation may fail */

    /* Run the OpenSSL init if the symbol was found. */
    if (ssl_fn.OPENSSL_init_crypto != NULL) {
        /* OPENSSL_INIT_LOAD_SSL_STRINGS = 0x00200000L */
        ssl_fn.OPENSSL_init_crypto(0x00200000L, NULL);
    }

    tls_initialized = 1;
    LOG_INFO("TLS subsystem initialised (OpenSSL dynamic link)");
}

/* ---------------------------------------------------------------------------
 * tls_conn: opaque handle holds SSL* and the raw socket fd
 * --------------------------------------------------------------------------- */

struct tls_conn {
    SSL_CTX *ctx;
    SSL     *ssl;
    int      sockfd;
};

/* ---------------------------------------------------------------------------
 * Internal: log the pending OpenSSL error queue entry
 * --------------------------------------------------------------------------- */

static void log_ssl_error(const char *context)
{
    unsigned long err = ssl_fn.ERR_get_error();
    if (err == 0) {
        LOG_ERROR_F("TLS error in %s (no OpenSSL error code)", context);
    } else {
        char buf[256];
        ssl_fn.ERR_error_string_n(err, buf, sizeof(buf));
        LOG_ERROR_F("TLS error in %s: %s", context, buf);
    }
}

/* ---------------------------------------------------------------------------
 * tls_connect
 * --------------------------------------------------------------------------- */

tls_conn *tls_connect(const char *host, const char *port)
{
    FATAL(!tls_initialized,
          "tls_connect called before tls_global_init — call tls_global_init() at startup");

    /* --- TCP resolution and connect --- */
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family   = AF_UNSPEC;   /* allow IPv4 or IPv6 */
    hints.ai_flags    = AI_NUMERICSERV | AI_ADDRCONFIG;

    struct addrinfo *res = NULL;
    int rc = getaddrinfo(host, port, &hints, &res);
    if (rc != 0) {
        LOG_ERROR_F("tls_connect: getaddrinfo(%s:%s) failed: %s",
                    host, port, gai_strerror(rc));
        FATAL(1, "tls_connect: DNS resolution failed");
    }

    int sockfd = -1;
    for (struct addrinfo *ai = res; ai != NULL; ai = ai->ai_next) {
        sockfd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (sockfd < 0) {
            continue;
        }
        if (connect(sockfd, ai->ai_addr, ai->ai_addrlen) == 0) {
            break;  /* connected */
        }
        close(sockfd);
        sockfd = -1;
    }
    freeaddrinfo(res);

    if (sockfd < 0) {
        LOG_ERROR_F("tls_connect: could not connect to %s:%s", host, port);
        FATAL(1, "tls_connect: TCP connection failed");
    }

    LOG_VERBOSE_F("tls_connect: TCP connected to %s:%s fd=%d", host, port, sockfd);

    /* --- TLS setup --- */
    const SSL_METHOD *method = ssl_fn.TLS_client_method();
    FATAL(method == NULL, "tls_connect: TLS_client_method() returned NULL");

    SSL_CTX *ctx = ssl_fn.SSL_CTX_new(method);
    FATAL(ctx == NULL, "tls_connect: SSL_CTX_new() failed");

    /* SSL_OP_NO_SSLv2 | SSL_OP_NO_SSLv3 = 0x01000000L | 0x02000000L */
    ssl_fn.SSL_CTX_set_options(ctx, 0x01000000L | 0x02000000L);

    /* Load system CA certificates (AL2023 ships /etc/pki/tls/certs/ca-bundle.crt). */
    rc = ssl_fn.SSL_CTX_set_default_verify_paths(ctx);
    if (rc != 1) {
        log_ssl_error("SSL_CTX_set_default_verify_paths");
        FATAL(1, "tls_connect: failed to load system CA certificates");
    }

    SSL *ssl = ssl_fn.SSL_new(ctx);
    FATAL(ssl == NULL, "tls_connect: SSL_new() failed");

    rc = ssl_fn.SSL_set_fd(ssl, sockfd);
    FATAL(rc != 1, "tls_connect: SSL_set_fd() failed");

    /* Send SNI hostname so virtual-hosted servers return the right certificate. */
    if (ssl_fn.SSL_set_tlsext_host_name != NULL) {
        rc = ssl_fn.SSL_set_tlsext_host_name(ssl, host);
        if (rc != 1) {
            log_ssl_error("SSL_set_tlsext_host_name");
            /* Non-fatal: SNI missing may cause cert mismatch on some hosts,
             * but we continue — the handshake failure below will FATAL. */
        }
    }

    /* Handshake. */
    rc = ssl_fn.SSL_connect(ssl);
    if (rc != 1) {
        log_ssl_error("SSL_connect");
        ssl_fn.SSL_free(ssl);
        ssl_fn.SSL_CTX_free(ctx);
        close(sockfd);
        FATAL(1, "tls_connect: TLS handshake failed");
    }

    LOG_VERBOSE_F("tls_connect: TLS handshake complete with %s:%s", host, port);

    tls_conn *conn = malloc(sizeof(tls_conn));
    FATAL(conn == NULL, "tls_connect: malloc(tls_conn) failed — OOM");
    conn->ctx    = ctx;
    conn->ssl    = ssl;
    conn->sockfd = sockfd;
    return conn;
}

/* ---------------------------------------------------------------------------
 * tls_send
 * --------------------------------------------------------------------------- */

int tls_send(tls_conn *conn, const char *buf, int len)
{
    int total_sent = 0;
    while (total_sent < len) {
        int rc = ssl_fn.SSL_write(conn->ssl, buf + total_sent, len - total_sent);
        if (rc > 0) {
            total_sent += rc;
            continue;
        }
        int err = ssl_fn.SSL_get_error(conn->ssl, rc);
        /* SSL_ERROR_WANT_READ = 2, SSL_ERROR_WANT_WRITE = 3 */
        if (err == 2 || err == 3) {
            /* Transient: retry. */
            continue;
        }
        log_ssl_error("SSL_write");
        return -1;
    }
    return total_sent;
}

/* ---------------------------------------------------------------------------
 * tls_recv
 * --------------------------------------------------------------------------- */

int tls_recv(tls_conn *conn, char *buf, int len)
{
    int rc = ssl_fn.SSL_read(conn->ssl, buf, len);
    if (rc > 0) {
        return rc;
    }
    if (rc == 0) {
        /* Clean shutdown by peer. */
        return 0;
    }
    int err = ssl_fn.SSL_get_error(conn->ssl, rc);
    /* SSL_ERROR_WANT_READ = 2, SSL_ERROR_WANT_WRITE = 3 */
    if (err == 2 || err == 3) {
        return -1;  /* transient; caller may retry */
    }
    /* SSL_ERROR_ZERO_RETURN = 6 — clean close */
    if (err == 6) {
        return 0;
    }
    log_ssl_error("SSL_read");
    return -1;
}

/* ---------------------------------------------------------------------------
 * tls_close
 * --------------------------------------------------------------------------- */

void tls_close(tls_conn *conn)
{
    if (conn == NULL) {
        return;
    }
    /* Best-effort close_notify — ignore errors (peer may have gone away). */
    ssl_fn.SSL_shutdown(conn->ssl);
    ssl_fn.SSL_free(conn->ssl);
    ssl_fn.SSL_CTX_free(conn->ctx);
    close(conn->sockfd);
    free(conn);
}
