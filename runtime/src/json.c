#include "json.h"
#include "runtime.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// Scan forward from json[pos] skipping whitespace. Returns new pos.
static size_t skip_ws(const char *json, size_t len, size_t pos) {
    while (pos < len &&
           (json[pos] == ' ' || json[pos] == '\t' ||
            json[pos] == '\r' || json[pos] == '\n')) {
        pos++;
    }
    return pos;
}

// Find the next occurrence of '"key"' (quoted, exact) inside json[0..len).
// Returns the index of the opening '"' of the key, or len if not found.
static size_t find_key(const char *json, size_t len,
                        const char *key, size_t key_len) {
    // We need to find: " key_bytes "
    // Search by sliding window: look for '"', then compare key_len bytes, then '"'.
    if (key_len == 0 || len < key_len + 2) {
        return len;
    }
    size_t i = 0;
    while (i + key_len + 1 < len) {
        if (json[i] == '"' &&
            memcmp(json + i + 1, key, key_len) == 0 &&
            json[i + 1 + key_len] == '"') {
            return i;
        }
        i++;
    }
    return len;
}

// ---------------------------------------------------------------------------
// json_get_str
// ---------------------------------------------------------------------------

char *json_get_str(const char *json, size_t json_len,
                   const char *key, size_t key_len,
                   size_t *out_len) {
    size_t key_pos = find_key(json, json_len, key, key_len);
    if (key_pos == json_len) {
        return NULL;
    }

    // Advance past the closing '"' of the key.
    size_t pos = key_pos + 1 + key_len + 1;

    // Skip whitespace, then require ':'.
    pos = skip_ws(json, json_len, pos);
    if (pos >= json_len || json[pos] != ':') {
        return NULL;
    }
    pos++;  // consume ':'

    // Skip whitespace, then require opening '"'.
    pos = skip_ws(json, json_len, pos);
    if (pos >= json_len || json[pos] != '"') {
        return NULL;
    }
    pos++;  // consume opening '"'

    // Scan to closing '"'. No escape handling — flat objects only.
    size_t val_start = pos;
    while (pos < json_len && json[pos] != '"') {
        pos++;
    }
    if (pos >= json_len) {
        return NULL;  // unterminated string
    }

    *out_len = pos - val_start;
    // Cast away const: caller receives a pointer into the original buffer,
    // which the caller owns. We do not modify it here.
    return (char *)(json + val_start);
}

// ---------------------------------------------------------------------------
// json_get_int
// ---------------------------------------------------------------------------

int64_t json_get_int(const char *json, size_t json_len,
                     const char *key, size_t key_len) {
    size_t key_pos = find_key(json, json_len, key, key_len);
    if (key_pos == json_len) {
        fprintf(stderr, "json_get_int: key \"%.*s\" not found\n",
                (int)key_len, key);
        exit(1);
    }

    // Advance past the closing '"' of the key.
    size_t pos = key_pos + 1 + key_len + 1;

    // Skip whitespace, then require ':'.
    pos = skip_ws(json, json_len, pos);
    if (pos >= json_len || json[pos] != ':') {
        fprintf(stderr, "json_get_int: malformed JSON, expected ':' after key \"%.*s\"\n",
                (int)key_len, key);
        exit(1);
    }
    pos++;  // consume ':'

    // Skip whitespace.
    pos = skip_ws(json, json_len, pos);
    if (pos >= json_len) {
        fprintf(stderr, "json_get_int: truncated JSON, no value for key \"%.*s\"\n",
                (int)key_len, key);
        exit(1);
    }

    // Handle optional leading '-'.
    int negative = 0;
    if (json[pos] == '-') {
        negative = 1;
        pos++;
    }

    if (pos >= json_len || json[pos] < '0' || json[pos] > '9') {
        fprintf(stderr, "json_get_int: expected digit for key \"%.*s\"\n",
                (int)key_len, key);
        exit(1);
    }

    int64_t result = 0;
    while (pos < json_len && json[pos] >= '0' && json[pos] <= '9') {
        result = result * 10 + (json[pos] - '0');
        pos++;
    }

    return negative ? -result : result;
}

// ---------------------------------------------------------------------------
// json_open
// ---------------------------------------------------------------------------

size_t json_open(char *buf, size_t pos) {
    buf[pos] = '{';
    return pos + 1;
}

// ---------------------------------------------------------------------------
// json_write_str
// ---------------------------------------------------------------------------

size_t json_write_str(char *buf, size_t pos,
                      const char *key, size_t key_len,
                      const char *val, size_t val_len) {
    // Comma separator if not the first field (pos > 1 means past the '{').
    if (pos > 1) {
        buf[pos++] = ',';
    }

    // "key"
    buf[pos++] = '"';
    memcpy(buf + pos, key, key_len);
    pos += key_len;
    buf[pos++] = '"';

    buf[pos++] = ':';

    // "val"
    buf[pos++] = '"';
    memcpy(buf + pos, val, val_len);
    pos += val_len;
    buf[pos++] = '"';

    return pos;
}

// ---------------------------------------------------------------------------
// json_write_int
// ---------------------------------------------------------------------------

size_t json_write_int(char *buf, size_t pos,
                      const char *key, size_t key_len,
                      int64_t val) {
    // Comma separator if not the first field.
    if (pos > 1) {
        buf[pos++] = ',';
    }

    // "key"
    buf[pos++] = '"';
    memcpy(buf + pos, key, key_len);
    pos += key_len;
    buf[pos++] = '"';

    buf[pos++] = ':';

    // Convert int64_t to decimal.  Handle 0, negative, and positive.
    if (val == 0) {
        buf[pos++] = '0';
        return pos;
    }

    if (val < 0) {
        buf[pos++] = '-';
        // Avoid UB on INT64_MIN: negate via unsigned arithmetic.
        uint64_t uval = (val == INT64_MIN)
            ? ((uint64_t)INT64_MAX + 1)
            : (uint64_t)(-val);

        // Write digits in reverse into a temp buffer.
        char tmp[20];
        int n = 0;
        while (uval > 0) {
            tmp[n++] = '0' + (char)(uval % 10);
            uval /= 10;
        }
        // Reverse into buf.
        for (int i = n - 1; i >= 0; i--) {
            buf[pos++] = tmp[i];
        }
    } else {
        uint64_t uval = (uint64_t)val;
        char tmp[20];
        int n = 0;
        while (uval > 0) {
            tmp[n++] = '0' + (char)(uval % 10);
            uval /= 10;
        }
        for (int i = n - 1; i >= 0; i--) {
            buf[pos++] = tmp[i];
        }
    }

    return pos;
}

// ---------------------------------------------------------------------------
// json_close
// ---------------------------------------------------------------------------

size_t json_close(char *buf, size_t pos) {
    buf[pos] = '}';
    return pos + 1;
}
