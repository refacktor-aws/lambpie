#include "arena.h"
#include <sys/mman.h>
#include <stdio.h>
#include <stdlib.h>

arena static_arena;
arena req_arena;

void arena_init(arena *a, size_t capacity) {
    a->base = mmap(NULL, capacity,
        PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE,
        -1, 0);
    if (a->base == MAP_FAILED) {
        fprintf(stderr, "arena_init: mmap failed\n");
        exit(1);
    }
    a->cursor = a->base;
    a->limit = a->base + capacity;
}

void *arena_alloc(arena *a, size_t size) {
    size = (size + 7) & ~7;  // 8-byte align
    char *ptr = a->cursor;
    a->cursor += size;
    return ptr;
}

void arena_reset(arena *a) {
    a->cursor = a->base;
}

void arena_freeze(arena *a) {
    // Make the used portion read-only. Writes after freeze cause SIGSEGV.
    size_t used = a->cursor - a->base;
    if (used > 0) {
        // Round up to page boundary for mprotect
        int pagesize = getpagesize();
        size_t prot_size = (used + pagesize - 1) & ~(pagesize - 1);
        mprotect(a->base, prot_size, PROT_READ);
    }
}

void *lambpie_arena_alloc(int tag, size_t size) {
    if (tag == ARENA_STATIC) {
        return arena_alloc(&static_arena, size);
    } else {
        return arena_alloc(&req_arena, size);
    }
}
