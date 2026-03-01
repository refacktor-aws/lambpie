# The Origin Story of Lambpie

## Prologue: Taipei

Lambpie began life as **taipei** — a statically typed, minimalist subset of
Python that compiles to LLVM IR. Taipei lived at `D:\git\taipei`, a ~588-line
compiler (`compiler.py`) using Python's `ast` module and `llvmlite`. Its thesis
was provocative: *the bloat and dynamism in current mainstream programming
languages makes them less than ideal for AI Coding Agents, and a simpler
statically-typed language makes a better target.* Type hints were mandatory,
compilation units were restricted to imports, classes, and a Main Marker, and
C interop came via `from C import printf, atoi`.

Taipei reached its first milestone — a fibonacci calculator compiled through
LLVM IR to a native executable — and then the question became: what's it *for*?

---

## Session 1: The Vision (Feb 28, 2026)

The user opened a Claude Code session in the taipei repo with a clear, ambitious
statement:

> "I want to create a new programming language, 'lambpie' (provisional name,
> need ideas). It'll be a fork of taipei and combine with
> `d:\git\aws-lambda-libc-runtime` to create 'a minimalist language for coding
> of highly-efficient AWS Lambda functions. It features AWS SDK as built-in
> language primitives (hence, no library bloat) and a strict dual-arena (static
> & request-time) memory management system. It is intended to be safe and
> highly efficient for solving simple problems, and its syntax provides an easy
> exit hatch to python when complexity grows.'"

The name "lambpie" was the user's own idea from the start — introduced as
"provisional" with a request for alternatives. Two agents were dispatched to
explore the source material:

- **Taipei**: the Python-subset compiler described above.
- **aws-lambda-libc-runtime**: an ultra-minimal C Lambda runtime (315 lines,
  ~5KB binary, 4-5ms cold starts) with raw HTTP handling, `mmap()`-based
  memory, and a `no_std`/`no_main` Rust binding.

### The Design Questions

Claude asked structured questions, and the user answered decisively:

**SDK design?** — Standard modules, boto3-like API. *"Only pay for what you
use."* Imports like `from aws.dynamodb import put_item` would link only the
referenced services into the final binary. *(Evolved: HTTP-only first was
chosen in Session 2 — raw HTTP + SigV4 as primitives, with Smithy codegen
pushed to M5. The boto3-like surface is now a later goal, not the foundation.)*

**Python escape hatch?** — Rename `.pie` to `.py`. Valid `.pie` files must be
valid Python 3. When complexity outgrows lambpie, just rename the file.
*(Evolved: `.pie` diverged from valid Python once arena tags and C interop
entered the picture. The escape hatch is now `escape.py`, a converter tool
that transforms `.pie` into runnable Python — not a simple rename.)*

**AWS services scope?** — All services, *"using the official (smithy?)
channels"* — but HTTP-only first, Smithy codegen later. *(Evolved: discovered
that boto3's botocore ships JSON service models
(`botocore/data/*/service-2.json`) containing every operation's shapes. No
Smithy parsing needed — botocore's JSON is already structured and sufficient.)*

**Memory model?** — Bump allocator with *"protections against forward-pointing
from static to reqtime."* Not just a bump allocator, but an explicit safety
rule: objects in the static arena (which persists across invocations) must never
reference objects in the request-time arena (which gets wiped), because that
would create dangling pointers. *(Evolved: the compile-time forward-pointer
check was killed during M2 — "the forward-pointer protection is redundant when
we have the static as readonly." `mprotect()` makes the static arena read-only
at runtime; any write SIGSEGVs. Hardware enforcement replaced software checks.)*

The user also fired off several rapid clarifications:

- *"(static and 'request-time')"* — correcting "runtime" arena to
  "request-time" arena, to distinguish from the Lambda runtime itself.
- *"The core runtime will be based on aws-lambda-libc-runtime's Rust binding."*
  — the linking layer would be the existing Rust `no_std`/`no_main` binding,
  not raw C via clang.
- *"The compiler will remain in python. Language self-hosting is not a goal."*
- *"create project in ../ and confirm name before writing any files."*

### Naming the Language

Claude offered alternatives:

| Name | Rationale |
|------|-----------|
| **lambpie** | Lambda + pie. "Lamb pie" is a real dish. Memorable, googleable. |
| **bake** | You "bake" your `.pie` into a binary. CLI: `bake handler.pie` |
| **shepherd** | Shepherd's pie. "Shepherds your Lambda functions." |
| **humble** | Humble pie. Emphasizes minimalism. |
| **lambic** | Lambda + IC (compiled). Also a Belgian beer. |

The user chose: **lambpie**.

The session ended with a complete six-milestone plan but no code written — the
user wanted to review the full design before any implementation began.

### Decisions Locked In

| Decision | Choice |
|----------|--------|
| Language name | lambpie |
| File extension | `.pie` (`escape.py` converts to valid Python) |
| Compiler | Stays in Python (no self-hosting) |
| Runtime base | Rust binding (`no_std`, `no_main`, no async) |
| Repo structure | Monorepo at `D:\git\lambpie` |
| Memory model | Dual-arena bump allocator (static + request-time) |
| Entry points | `Handler.init()` + `Handler.handle()` |
| JSON | Typed structs with compiler-generated codecs |

---

## Session 2: Building It (same day, ~10 min later)

The user returned and said: **"Implement the following plan."**

### TLS and SigV4 — First Debate

Before any code was written, the user asked about TLS:

> "for the TLS, is there a more minimal version of openssl? how big is openssl?
> Can we dynamic-link to openssl?"

Three options: dynamic-link OpenSSL from AL2023 (zero binary cost), static-link
BearSSL (~20KB, no TLS 1.3), or static-link mbedTLS (~60-100KB). The user's
call was compact:

> "dynamic-link openssl, pure Rust sha256. now start implementing milestone 1"

### Milestone 1: Echo Handler (~7 minutes)

Claude created the entire `D:\git\lambpie` repo from scratch — 19 files:

- **`compiler.py`** — forked from taipei. `_synthesize_main()` replaced by
  `_synthesize_lambda_entry()`, emitting `lambpie_init()` and
  `lambpie_handle()` as `extern "C"` functions.
- **`echo.pie`** — the first handler: copies event bytes to response via memcpy.
- **`builtins.pie`** — renamed from `builtins.tpy`.
- **Runtime**: `runtime.c/h` from aws-lambda-libc-runtime, Rust binding with
  Writer API, shim crate with `_start()`.
- **Scripts**: `build.py`, `deploy.py`, `test.py`.
- **Tests**: `test_compiler.py` with 3 tests.

Hiccups: `FileNotFoundError` on `target/echo.ll` (fixed with `os.makedirs`),
and llvmlite quoting `@"lambpie_init"` vs `@lambpie_init` (tests updated).

All 3 tests passed. First commit: `47c34c4`. **M1 done in ~7 minutes.**

### The Agent Team

Claude proposed 5 specialist sub-agents (Compiler, Rust Runtime, C Runtime, AWS
Integration, Build Toolchain). The user approved. But when they ran as ephemeral
tasks with no persistent files, the user noticed:

> "How come I don't see any files with the agent definitions?"

Persistent agent definitions were created in `.claude/agents/` with YAML
frontmatter.

### Forward-Pointer Protection — Killed

During M2, the user cut the compile-time forward-pointer check:

> "the forward-pointer protection is redundant when we have the static as
> readonly"

Since `mprotect()` makes the static arena read-only at runtime, any write
to it during `handle()` would SIGSEGV — stronger than any compile-time check.
Removed.

### Milestone 2: Dual-Arena Bump Allocator

- `arena.c`/`arena.h` with dual-arena bump allocation.
- `arena_freeze()` via `mprotect()` for read-only static arena.
- Compiler rewritten: `lambpie_arena_alloc(tag, size)` instead of `malloc`,
  with context-aware arena tag switching.
- Shim updated: init arenas, freeze static after `lambpie_init()`, reset
  request arena after each handler call.

All 7 tests passed. Committed: `640b6b2`. **M2 done.**

### The Code Quality Crackdown

The user spotted problems:

> "I see sloppy code, e.g. fallback to dumb default. Update the CLAUDE.md to
> prevent this"

Offenses catalogued:
- Unknown `from C import` silently got a `void()` signature — wrong behavior
  disguised as a default.
- `else: pass` swallowing unknown imports silently.
- Debug `print()` statements in production paths.
- `abs(hash(node.value))` for string naming — hash collisions = wrong IR.
- Missing Handler class only printed a warning and continued.
- Hardcoded `MemorySize=128` in deploy.py.

All fixed. CLAUDE.md updated: **"Fail fast and hard, always — no silent
fallbacks, no warning-and-continue, no hidden defaults."**

### C Signatures — The Demand

Final ask of the session:

> "C_SIGNATURES must be parsed from libc header files and stored in a yaml file"

The plan: Docker + `pycparser` to extract function signatures from AL2023 glibc
headers into YAML, loaded by the compiler at startup. Session ended with plan
queued.

---

## Session 3: The YAML Pipeline (same day, continued)

This session picked up the C signatures work, mistakenly working in the
upstream taipei repo instead of lambpie.

### scripts/parse_libc.py — Created

The script: run `amazonlinux:2023` in Docker, install gcc, preprocess headers
with `gcc -E`, parse with `pycparser`, map C types to LLVM IR types, write
`c_signatures.yaml`.

### Three Bugs, Three Fixes

1. **Bash syntax error** — `-D__attribute__(x)=` had parentheses that bash
   inside Docker parsed as syntax. Fixed by piping a full shell script via
   stdin instead of `-c "..."`.

2. **Windows CRLF in Linux container** — `subprocess.run(text=True)` on Windows
   sends `\r\n`, breaking `set -e` inside the container. Fixed by encoding as
   explicit UTF-8 bytes.

3. **`__builtin_va_list` expansion** — `-D__builtin_va_list=void *` caused
   `typedef void * void *;`. Fixed with `-D__builtin_va_list=int`.

pycparser still choked on remaining glibc constructs. The user suggested
tree-sitter as a fallback. Session suspended, this origin story written.

---

## Session 3.5: End-to-End Build Pipeline (same day)

The Docker multi-stage build and localstack verification pipeline were created:

- **`Dockerfile.build`** — Multi-stage: amazonlinux:2023 + llvmlite + LLVM 15 +
  Rust stable. Compiles `.pie` to LLVM IR, assembles to `.o`, links via cargo
  into the final `bootstrap` binary (8.5 KB ELF, dynamically linked).
- **`docker-compose.yml`** — localstack:3.0 on port 4577 (avoiding conflict with
  other projects on 4566).
- **`Makefile`** — orchestrates `test`, `build`, `package`, `verify`, `clean`.
- **`scripts/package.py`** — zips bootstrap with executable permissions.
- **`scripts/verify.py`** — boto3 deploys to localstack, invokes echo handler,
  asserts exact JSON match.

Bug fixes: `arena.c` needed `#define _GNU_SOURCE` and `#include <unistd.h>` for
MAP_ANONYMOUS/MAP_NORESERVE/getpagesize(). Lambda functions need a wait loop for
Active state before invoke (localstack creates in Pending state).

Docker build is fully cached after first run — sub-second rebuilds if only `.pie`
files change. **Milestone 3 done:** `make verify` passes end-to-end.

---

## Session 4: Flat Module Model + Typed Events (same day)

The user's feedback evolved across four messages, each one sharpening the design:

1. **Drop `class Handler`** — *"just have init code followed by def handle!"*
2. **Strongly typed events** — `class Request` with typed fields, not loose `dict`
3. **Compiler-generated JSON marshaling** — user never sees pointers
4. **Metadata for deployment** — compiler emits trigger type info

### The Problem

The existing handler API was unusable for real work — raw `__ptr__` pointers,
manual `memcpy`, C-level buffer management:

```python
# Old: raw pointers, manual buffer copy
class Handler:
    def handle(self, event_ptr: __ptr__, event_len: int,
               response_ptr: __ptr__, response_cap: int) -> int:
        memcpy(response_ptr, event_ptr, event_len)
        return event_len
```

### The New Design

```python
# New: typed events, compiler-generated JSON marshaling
class Request:
    message: str
    number: int

class Response:
    status: str
    echo: str
    doubled: int

def handle(event: Request) -> Response:
    return Response("ok", event.message, event.number + event.number)
```

The runtime FFI contract is unchanged — `lambpie_handle(event_ptr, event_len,
response_ptr, response_cap) -> i64` still receives and returns raw bytes. But
now the *compiler* generates the JSON parsing/serialization glue, so the user
never touches pointers.

### Implementation (8 Phases)

**Phase 1: C Runtime JSON helpers** — `json.h`/`json.c` with minimal,
non-allocating JSON parse/serialize for flat objects. Zero-copy string extraction,
integer parsing, and position-based serialization (`json_open`, `json_write_str`,
`json_write_int`, `json_close`).

**Phase 2: Field access** — `visit_Attribute` gained a field access fallback:
try method lookup first (existing), then GEP + load for struct fields. This
enables `event.message`, `event.number`.

**Phase 3: Auto-generated `__init__`** — If a class has annotated fields but no
explicit `__init__`, the compiler generates one taking positional args in field
order. This enables `Response("ok", event.message, 42)`.

**Phase 4: String literal coercion** — When constructing a class, if a field
expects `%str*` but the argument is `i8*` (a string literal), the compiler
automatically wraps it: allocates a `str` struct, sets buffer pointer and length.
This enables `Response("ok", ...)` without manual wrapping.

**Phase 5: Flat module model** — `visit_Module` rewritten: `ClassDef` and
`ImportFrom` are visited immediately for type registration. `def handle` is
deferred. Top-level statements become `lambpie_init()` body. No more
`class Handler` requirement.

**Phase 6: Rewrite `_synthesize_lambda_entry`** — Complete rewrite. `lambpie_init`
compiles collected init statements. `lambpie_handle` deserializes event JSON into
a typed struct (using the C JSON helpers), calls the user's `handle()` function,
and serializes the response struct back to JSON. Also emits metadata
(`target/<name>.lambpie.json`) with trigger type info — the extension point for
future AWS event types.

**Phase 7: Tests** — All tests rewritten for the new model. `echo.pie` becomes
the typed handler above. `verify.py` expects
`{"status":"ok","echo":"hello from lambpie","doubled":84}`.

**Phase 8: Documentation** — This section. CLAUDE.md updated.

### Decisions Locked In

| Decision | Choice |
|----------|--------|
| Handler model | Flat module: top-level `def handle(event: T) -> R` |
| Event types | User-defined classes with typed fields |
| JSON marshaling | Compiler-generated, using C runtime helpers |
| Metadata | `.lambpie.json` alongside `.ll` — trigger type for deploy |
| Extension point | Future: `SQSEvent`, `APIGatewayEvent` as known types |

---

## What Exists Today

**Done:**
- M1: Lambda echo handler compiles to LLVM IR, all tests pass
- M2: Dual-arena bump allocator with mprotect freeze, all tests pass
- M3: End-to-end build pipeline with Docker, `make verify` passes
- M4: Flat module model with typed events and compiler-generated JSON marshaling
- Specialist agent definitions in `.claude/agents/`
- Coding standards in CLAUDE.md

**TODO:**
- Raw HTTP + SigV4 signing (pure Rust SHA-256, dynlink OpenSSL for TLS)
- AWS SDK modules from botocore JSON models
- Python compatibility package (`escape.py`)
- C signatures YAML pipeline (Docker preprocessing works, parser needs fixing)

---

*Chronicled Feb 28, 2026*
