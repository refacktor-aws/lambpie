# Lambpie

A minimalist compiled language for AWS Lambda. Valid `.pie` is valid Python 3.

## Build & Test

```bash
# Compile .pie to LLVM IR
python compiler.py tests/echo.pie -o target/echo

# Run compiler unit tests
python -m pytest tests/test_compiler.py -v

# Full build pipeline (.pie -> bootstrap binary)
python scripts/build.py tests/echo.pie

# Deploy to AWS Lambda
python scripts/deploy.py --function-name my-func --build tests/echo.pie
```

## Architecture

```
handler.pie → compiler.py → handler.ll → handler.o → shim crate → bootstrap
```

- **compiler.py**: Python `ast` + `llvmlite`. Emits `lambpie_init()` + `lambpie_handle()` as extern "C". Auto-generates JSON marshaling.
- **runtime/src/**: C runtime (runtime.c — HTTP, sockets, Lambda API protocol; json.c — non-allocating JSON parse/serialize).
- **runtime/rust-binding/**: `no_std` Rust FFI wrapper around the C runtime.
- **runtime/shim/**: `no_std`, `no_main` crate. Provides `_start()`, links handler.o, bridges event loop.
- **scripts/**: build.py, deploy.py (boto3), test.py.

## Handler Convention (Flat Module Model)

```python
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

- Define event/response as classes with typed fields (`str`, `int`)
- Top-level `def handle(event: T) -> R` is the entry point
- Compiler auto-generates `__init__` for data classes (no need to write one)
- Compiler generates JSON deserialization (event) and serialization (response)
- String literals auto-coerce to `str` structs
- Top-level statements (outside `handle`) become `lambpie_init()` body
- Compiler emits `target/<name>.lambpie.json` metadata alongside `.ll`

## Memory Model (Dual-Arena)

- **Static arena** (tag 0): allocations in top-level init code, persists across invocations, frozen after init via mprotect
- **Request arena** (tag 1): allocations in `handle()`, bulk-reset after each invocation
- Forward-pointer protection is provided by mprotect (static arena read-only = runtime SIGSEGV on write)

## Coding Standards

**Fail fast and hard, always.** If something is unrecognized, unknown, or missing — raise an error immediately. Never substitute a dumb default, never print a warning and continue, never silently swallow. A compile error is always better than wrong IR. A missing CLI argument is always better than a hidden default that surprises in production.

Banned patterns:
- Fallback to a dummy type/value for unrecognized input (e.g. `void()` for unknown C import)
- `else: pass` that silently ignores an unhandled case
- `print("Warning: ...")` followed by `return` instead of `raise`
- Hardcoded magic numbers buried in code (e.g. `MemorySize=128`) — make them explicit arguments

**No debug print in production paths.** Use `raise` or `sys.exit(msg)`. Gate tracing behind `--verbose`.

**No hash-based naming.** `abs(hash(x))` collides. Use a monotonic counter.

## Key Decisions

- TLS: dynamic-link OpenSSL from AL2023 (zero binary cost)
- SigV4: implement in .pie (pure integer math — SHA-256 is 32-bit rotations/XORs)
- SDK models: parse botocore JSON models (not Smithy)
- Compiler stays in Python (no self-hosting)
- Cross-compile target: x86_64-unknown-linux-gnu
- Deploy via boto3, not awscli

## Subagents

Specialist agents are defined in `.claude/agents/`. They are auto-loaded by Claude Code:

### Management
1. **fpm** — Functional PM: top-level orchestrator, loops tpm + qa-tester until done
2. **tpm** — Technical PM: assesses project state, produces prioritized top-10 work items
3. **qa-tester** — QA: runs all tests, checks quality, reports pass/fail

### Specialists
4. **compiler-specialist** — compiler.py, builtins.pie, type system, AST visitors, codegen
5. **rust-runtime** — runtime/rust-binding/, runtime/shim/, no_std FFI, Writer API
6. **c-runtime** — runtime/src/, HTTP protocol, arena allocator, Lambda API
7. **aws-integration** — scripts/deploy.py, SigV4, botocore models, boto3 shims
8. **build-toolchain** — scripts/build.py, cross-compilation, llc, cargo, Docker
