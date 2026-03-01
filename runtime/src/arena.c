#define _GNU_SOURCE

#include "arena.h"
#include <sys/mman.h>
#include <unistd.h>
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
    size = (size + 7) & ~7;  /* 8-byte align */
    if (a->cursor + size > a->limit) {
        fprintf(stderr, "arena_alloc: arena exhausted (requested %zu bytes, %td available)\n",
                size, a->limit - a->cursor);
        exit(1);
    }
    char *ptr = a->cursor;
    a->cursor += size;
    return ptr;
}

void arena_reset(arena *a) {
    a->cursor = a->base;
}

void arena_freeze(arena *a) {
    /*
     * Make the ENTIRE arena mapping read-only, not just the used portion.
     *
     * Original code only protected (cursor - base) bytes rounded up to a page.
     * That left the unused tail (cursor..limit) still writable, so a post-freeze
     * arena_alloc() would silently succeed if the bump landed in the unprotected
     * tail.  Protecting the full capacity makes any write — to used or unused
     * bytes — fault immediately, which is the intended invariant.
     *
     * mprotect requires page-aligned length; the arena was mmap'd with MAP_ANONYMOUS
     * so its size is always a multiple of the page size.
     */
    size_t capacity = (size_t)(a->limit - a->base);
    if (capacity > 0) {
        int rc = mprotect(a->base, capacity, PROT_READ);
        if (rc != 0) {
            fprintf(stderr, "arena_freeze: mprotect failed\n");
            exit(1);
        }
    }
}

void *lambpie_arena_alloc(int tag, size_t size) {
    if (tag == ARENA_STATIC) {
        return arena_alloc(&static_arena, size);
    } else {
        return arena_alloc(&req_arena, size);
    }
}
