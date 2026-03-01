/*
 * arena_stress_test.c — Multi-invocation arena stability test
 *
 * Tests the dual-arena memory model used by the lambpie runtime:
 *   - Static arena (tag 0): allocations from lambpie_init(), frozen after init
 *   - Request arena (tag 1): allocations from lambpie_handle(), reset each invocation
 *
 * Verifications:
 *   1. Request arena cursor returns to base after each arena_reset()
 *   2. Static arena is fully read-only after arena_freeze() — both used and
 *      unused bytes (the bug fix: original code left the unused tail writable)
 *   3. No memory leak — arena capacity stays constant across N invocations
 *   4. 8-byte alignment invariant on every allocation
 *   5. Arena exhaustion detection — alloc past limit must call exit(1)
 *
 * SIGSEGV verification (Linux / mprotect):
 *   A POSIX signal handler catches SIGSEGV with SA_SIGINFO.  The test writes
 *   to the frozen static arena and expects a fault.  If no fault arrives within
 *   the test, the check fails hard.
 *
 * Usage: compiled and run by scripts/test_arena.py inside Docker.
 * Can also be run directly on any Linux host with:
 *   cc -std=c17 -Wall -Wextra -o arena_stress_test \
 *      runtime/tests/arena_stress_test.c runtime/src/arena.c \
 *      -I runtime/src && ./arena_stress_test
 */

#define _GNU_SOURCE

#include "../src/arena.h"

#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

/* -------------------------------------------------------------------------
 * Test scaffolding
 * ------------------------------------------------------------------------- */

static int tests_run    = 0;
static int tests_passed = 0;
static int tests_failed = 0;

#define CHECK(label, cond) do {                                         \
    tests_run++;                                                        \
    if (cond) {                                                         \
        tests_passed++;                                                 \
        printf("  PASS  %s\n", label);                                  \
    } else {                                                            \
        tests_failed++;                                                  \
        fprintf(stderr, "  FAIL  %s  (line %d)\n", label, __LINE__);   \
    }                                                                   \
} while (0)

#define FATAL_TEST(cond, msg) do {                                      \
    if (cond) {                                                         \
        fprintf(stderr, "FATAL: %s (line %d)\n", msg, __LINE__);       \
        exit(1);                                                        \
    }                                                                   \
} while (0)

/* -------------------------------------------------------------------------
 * Helper: make an arena from scratch on a private anonymous mapping.
 * Does NOT use the global static_arena / req_arena so tests are isolated.
 * ------------------------------------------------------------------------- */

static void make_arena(arena *a, size_t capacity)
{
    arena_init(a, capacity);
    FATAL_TEST(a->base == NULL || a->base == (char *)MAP_FAILED,
               "make_arena: arena_init returned bad base");
    FATAL_TEST(a->cursor != a->base,
               "make_arena: cursor not at base after init");
    FATAL_TEST(a->limit != a->base + capacity,
               "make_arena: limit does not match capacity");
}

/* Unmap an arena's backing store (cleanup after isolated tests). */
static void destroy_arena(arena *a)
{
    size_t capacity = (size_t)(a->limit - a->base);
    if (capacity > 0 && a->base != NULL && a->base != (char *)MAP_FAILED) {
        munmap(a->base, capacity);
    }
    a->base = a->cursor = a->limit = NULL;
}

/* -------------------------------------------------------------------------
 * Test 1: Cursor arithmetic — reset returns to base
 * ------------------------------------------------------------------------- */

static void test_cursor_reset(void)
{
    printf("\n[Test 1] Cursor arithmetic — arena_reset returns cursor to base\n");

    const size_t CAPACITY   = 256 * 1024;  /* 256 KB */
    const int    N_INVOCATIONS = 100;

    arena req;
    make_arena(&req, CAPACITY);

    char *const base = req.base;

    long total_allocs = 0;
    size_t prev_capacity = (size_t)(req.limit - req.base);

    for (int inv = 0; inv < N_INVOCATIONS; inv++) {

        /* Simulate a request: allocate varying amounts */
        size_t alloc_this_round = 0;
        for (int i = 0; i < 7; i++) {
            size_t sz = (size_t)(16 + i * 32);  /* 16, 48, 80, 112, ... bytes */
            void *p = arena_alloc(&req, sz);

            /* Each allocation must be 8-byte aligned */
            FATAL_TEST(((uintptr_t)p & 7) != 0,
                       "allocation is not 8-byte aligned");

            /* Pointer must be within arena bounds */
            FATAL_TEST((char *)p < req.base || (char *)p >= req.limit,
                       "allocation pointer is out of arena bounds");

            alloc_this_round += (sz + 7) & ~7;
            total_allocs++;
        }

        /* Cursor advanced correctly */
        CHECK("cursor advanced after allocs",
              (size_t)(req.cursor - base) == alloc_this_round);

        /* arena_reset: cursor must return to base exactly */
        arena_reset(&req);
        CHECK("cursor at base after reset", req.cursor == base);

        /* Capacity must be unchanged — no leak */
        size_t current_capacity = (size_t)(req.limit - req.base);
        CHECK("arena capacity unchanged after reset",
              current_capacity == prev_capacity);
        prev_capacity = current_capacity;
    }

    printf("  Total allocations across %d invocations: %ld\n",
           N_INVOCATIONS, total_allocs);
    printf("  Final capacity: %zu bytes\n",
           (size_t)(req.limit - req.base));

    destroy_arena(&req);
}

/* -------------------------------------------------------------------------
 * Test 2: Alignment invariant — all sizes, including odd ones
 * ------------------------------------------------------------------------- */

static void test_alignment(void)
{
    printf("\n[Test 2] Alignment invariant — all allocations are 8-byte aligned\n");

    /* Sum of aligned sizes 1..512: ~130 KB.  Use 160 KB with headroom. */
    const size_t CAPACITY = 160 * 1024;
    arena a;
    make_arena(&a, CAPACITY);

    int misaligned = 0;
    for (size_t sz = 1; sz <= 512; sz++) {
        void *p = arena_alloc(&a, sz);
        if (((uintptr_t)p & 7) != 0) {
            misaligned++;
            fprintf(stderr, "  MISALIGNED: size=%zu ptr=%p\n", sz, p);
        }
    }
    CHECK("all sizes 1-512 produce 8-byte-aligned pointers", misaligned == 0);

    destroy_arena(&a);
}

/* -------------------------------------------------------------------------
 * Test 3: Static arena frozen — used and unused bytes both protected
 * ------------------------------------------------------------------------- */

/*
 * We use fork() to test that writing to the frozen arena causes SIGSEGV.
 * The child process:
 *   a) Allocates some bytes from the static arena.
 *   b) Calls arena_freeze().
 *   c) Attempts a write to a frozen byte.
 *   d) If the write did NOT fault, the child exits with code 42 (test failure).
 *   e) If the write faulted (SIGSEGV), the child is killed by signal — the
 *      parent checks WIFSIGNALED(status) && WTERMSIG(status) == SIGSEGV.
 *
 * We run two variants:
 *   (a) Write to used portion  (was protected even in the old code)
 *   (b) Write to unused tail   (was NOT protected by the old code — our fix)
 */

typedef enum { USED_PORTION, UNUSED_TAIL } freeze_test_variant;

static void freeze_child(freeze_test_variant variant)
{
    const size_t PAGE = (size_t)getpagesize();
    const size_t CAPACITY = PAGE * 4;  /* 4 pages */

    arena a;
    make_arena(&a, CAPACITY);

    /* Allocate exactly one page worth of data (stays in first page). */
    char *p = arena_alloc(&a, PAGE);

    /* Write a sentinel to confirm the allocation is live before freeze. */
    memset(p, 0xAB, PAGE);

    arena_freeze(&a);

    /* Now attempt the write — this should SIGSEGV. */
    volatile char *target;
    if (variant == USED_PORTION) {
        /* Write into the used (frozen) portion. */
        target = (volatile char *)a.base;
    } else {
        /* Write into the unused tail — this is the byte immediately after
         * the used portion (still within the mmap'd region).
         * The old arena_freeze() would NOT have mprotect'd this byte. */
        target = (volatile char *)(a.base + PAGE);  /* first byte of unused tail */
    }

    *target = 0xFF;  /* should SIGSEGV */

    /* If we reach here the write succeeded — the arena is NOT frozen. */
    fprintf(stderr, "  CHILD: write to %s did NOT fault — arena is not frozen!\n",
            variant == USED_PORTION ? "used portion" : "unused tail");
    exit(42);  /* 42 = sentinel for "no fault received" */
}

static void run_freeze_subtest(freeze_test_variant variant, const char *label)
{
    pid_t pid = fork();
    FATAL_TEST(pid < 0, "fork() failed");

    if (pid == 0) {
        /* Child */
        freeze_child(variant);
        /* freeze_child either exits(42) or is killed by SIGSEGV */
        exit(0);  /* unreachable */
    }

    /* Parent: wait for child */
    int status = 0;
    pid_t waited = waitpid(pid, &status, 0);
    FATAL_TEST(waited != pid, "waitpid() returned wrong pid");

    int faulted_as_expected = 0;
    if (WIFSIGNALED(status)) {
        int sig = WTERMSIG(status);
        if (sig == SIGSEGV || sig == SIGBUS) {
            faulted_as_expected = 1;
        } else {
            fprintf(stderr, "  Child killed by unexpected signal %d\n", sig);
        }
    } else if (WIFEXITED(status)) {
        int code = WEXITSTATUS(status);
        if (code == 42) {
            fprintf(stderr, "  Child: write succeeded — arena NOT frozen\n");
        } else {
            fprintf(stderr, "  Child exited with unexpected code %d\n", code);
        }
    }

    CHECK(label, faulted_as_expected);
}

static void test_freeze_protection(void)
{
    printf("\n[Test 3] arena_freeze — both used and unused bytes become read-only\n");

    run_freeze_subtest(USED_PORTION,
        "write to used portion after freeze causes SIGSEGV");
    run_freeze_subtest(UNUSED_TAIL,
        "write to unused tail after freeze causes SIGSEGV (requires fix)");
}

/* -------------------------------------------------------------------------
 * Test 4: Static arena content survives across N simulated invocations
 * ------------------------------------------------------------------------- */

static void test_static_content_survives(void)
{
    printf("\n[Test 4] Static arena — content survives N request-arena resets\n");

    const size_t STATIC_CAP = 64 * 1024;
    const size_t REQ_CAP    = 256 * 1024;
    const int    N          = 100;

    arena sa, ra;
    make_arena(&sa, STATIC_CAP);
    make_arena(&ra, REQ_CAP);

    /* Simulate lambpie_init(): allocate a "config" object in the static arena */
    const char *config_str = "lambpie-config-sentinel";
    size_t config_len = strlen(config_str) + 1;
    char *config = arena_alloc(&sa, config_len);
    memcpy(config, config_str, config_len);

    /* Freeze static arena (our fixed version protects all of it) */
    arena_freeze(&sa);

    char *sa_base   = sa.base;
    char *sa_cursor = sa.cursor;
    char *sa_limit  = sa.limit;

    int content_ok = 1;

    for (int inv = 0; inv < N; inv++) {
        /* Simulate a request: allocate from request arena */
        void *req_buf = arena_alloc(&ra, 1024);
        memset(req_buf, (int)(inv & 0xFF), 1024);  /* dirty the memory */

        /* Verify static arena fields are unchanged */
        if (sa.base != sa_base || sa.cursor != sa_cursor || sa.limit != sa_limit) {
            fprintf(stderr, "  Static arena struct modified during invocation %d\n", inv);
            content_ok = 0;
        }

        /* Verify static content is readable and correct */
        if (strcmp(config, config_str) != 0) {
            fprintf(stderr, "  Static content corrupted at invocation %d\n", inv);
            content_ok = 0;
        }

        arena_reset(&ra);
    }

    CHECK("static arena struct unchanged across 100 invocations", content_ok);
    CHECK("static arena content readable after 100 arena_resets",
          strcmp(config, config_str) == 0);

    /* Restore write permission so destroy_arena can munmap cleanly */
    mprotect(sa.base, (size_t)(sa.limit - sa.base), PROT_READ | PROT_WRITE);
    destroy_arena(&sa);
    destroy_arena(&ra);
}

/* -------------------------------------------------------------------------
 * Test 5: Arena exhaustion — alloc past limit must abort, not corrupt
 * ------------------------------------------------------------------------- */

/*
 * We fork() a child that tries to allocate more than the arena capacity.
 * The child must exit(1) (arena_alloc calls exit(1) on exhaustion).
 * It must NOT exit(0) (that would mean a silent overflow).
 */
static void test_exhaustion_detection(void)
{
    printf("\n[Test 5] Arena exhaustion — over-allocation is detected and aborted\n");

    pid_t pid = fork();
    FATAL_TEST(pid < 0, "fork() failed");

    if (pid == 0) {
        /* Child: allocate exactly capacity, then one byte more */
        const size_t CAPACITY = 4096;
        arena a;
        make_arena(&a, CAPACITY);

        /* Fill it up exactly */
        arena_alloc(&a, CAPACITY);

        /* This must call exit(1) inside arena_alloc */
        arena_alloc(&a, 1);

        /* If we reach here, exhaustion was NOT detected */
        fprintf(stderr, "  CHILD: over-allocation did not abort — silent overflow!\n");
        exit(0);  /* 0 = no abort = test failure */
    }

    int status = 0;
    waitpid(pid, &status, 0);

    int aborted_correctly = 0;
    if (WIFEXITED(status) && WEXITSTATUS(status) == 1) {
        aborted_correctly = 1;  /* arena_alloc called exit(1) */
    } else if (WIFSIGNALED(status)) {
        /* Also acceptable — it crashed rather than continuing silently */
        aborted_correctly = 1;
    }

    CHECK("over-allocation causes immediate abort (not silent overflow)",
          aborted_correctly);
}

/* -------------------------------------------------------------------------
 * Test 6: Multi-invocation stats report
 * ------------------------------------------------------------------------- */

static void test_stats_report(void)
{
    printf("\n[Test 6] Multi-invocation stats summary\n");

    const size_t STATIC_CAP = 64 * 1024;
    const size_t REQ_CAP    = 256 * 1024;
    const int    N          = 100;

    arena sa, ra;
    make_arena(&sa, STATIC_CAP);
    make_arena(&ra, REQ_CAP);

    /* Static init: 3 allocations */
    arena_alloc(&sa, 100);
    arena_alloc(&sa, 200);
    arena_alloc(&sa, 300);
    size_t static_used = (size_t)(sa.cursor - sa.base);

    arena_freeze(&sa);

    long total_allocs  = 0;
    long total_resets  = 0;
    size_t peak_req_used = 0;

    for (int inv = 0; inv < N; inv++) {
        /* Allocate a varying number of request-scoped objects */
        int n_allocs = 3 + (inv % 5);  /* 3..7 per invocation */
        size_t alloc_sz = 64;

        for (int i = 0; i < n_allocs; i++) {
            arena_alloc(&ra, alloc_sz);
            total_allocs++;
        }

        size_t req_used = (size_t)(ra.cursor - ra.base);
        if (req_used > peak_req_used) peak_req_used = req_used;

        arena_reset(&ra);
        total_resets++;

        /* After reset, cursor must be at base */
        FATAL_TEST(ra.cursor != ra.base,
                   "cursor not at base after reset — arena_reset broken");
    }

    printf("  Static arena : %zu bytes used / %zu capacity\n",
           static_used, STATIC_CAP);
    printf("  Request arena: %zu bytes peak / %zu capacity\n",
           peak_req_used, REQ_CAP);
    printf("  Total allocs : %ld  (across %d invocations)\n",
           total_allocs, N);
    printf("  Total resets : %ld\n", total_resets);
    printf("  Final req cursor at base: %s\n",
           ra.cursor == ra.base ? "YES" : "NO");

    CHECK("final request arena cursor is at base", ra.cursor == ra.base);
    CHECK("total resets equals invocation count", total_resets == N);

    mprotect(sa.base, (size_t)(sa.limit - sa.base), PROT_READ | PROT_WRITE);
    destroy_arena(&sa);
    destroy_arena(&ra);
}

/* -------------------------------------------------------------------------
 * main
 * ------------------------------------------------------------------------- */

int main(void)
{
    printf("=== lambpie arena stress test ===\n");
    printf("Page size: %d bytes\n", getpagesize());

    test_cursor_reset();
    test_alignment();
    test_freeze_protection();
    test_static_content_survives();
    test_exhaustion_detection();
    test_stats_report();

    printf("\n=== Results: %d/%d passed", tests_passed, tests_run);
    if (tests_failed > 0) {
        printf(", %d FAILED", tests_failed);
    }
    printf(" ===\n");

    return tests_failed > 0 ? 1 : 0;
}
