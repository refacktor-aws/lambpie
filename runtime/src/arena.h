#ifndef ARENA_H
#define ARENA_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    char *base;
    char *cursor;
    char *limit;
} arena;

// Tags for dual-arena system
#define ARENA_STATIC 0
#define ARENA_REQ    1

void  arena_init(arena *a, size_t capacity);
void *arena_alloc(arena *a, size_t size);
void  arena_reset(arena *a);
void  arena_freeze(arena *a);  // mprotect to read-only

// Global arenas (initialized by lambpie runtime)
extern arena static_arena;
extern arena req_arena;

// C-ABI entry point called by compiled .pie code
void *lambpie_arena_alloc(int tag, size_t size);

#endif
