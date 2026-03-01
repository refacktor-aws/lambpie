#ifndef JSON_H
#define JSON_H

#include <stdint.h>
#include <stddef.h>

// --- Parsing (zero-copy into json buffer) ---

// Find string value for key, return pointer into json buffer, set *out_len.
// Returns NULL if key not found. Does NOT null-terminate.
char *json_get_str(const char *json, size_t json_len,
                   const char *key, size_t key_len,
                   size_t *out_len);

// Find integer value for key. Fails hard if key not found.
int64_t json_get_int(const char *json, size_t json_len,
                     const char *key, size_t key_len);

// --- Serialization (position-based, no allocation) ---
// All take (buf, pos) and return new pos.

// Write opening '{'
size_t json_open(char *buf, size_t pos);

// Write "key":"val" (with comma if pos > 1)
size_t json_write_str(char *buf, size_t pos,
                      const char *key, size_t key_len,
                      const char *val, size_t val_len);

// Write "key":val (integer, with comma if pos > 1)
size_t json_write_int(char *buf, size_t pos,
                      const char *key, size_t key_len,
                      int64_t val);

// Write closing '}'
size_t json_close(char *buf, size_t pos);

#endif
