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

- **compiler.py**: Python `ast` + `llvmlite`. Emits `lambpie_init()` + `lambpie_handle()` as extern "C".
- **runtime/src/**: C runtime (runtime.c) — HTTP, sockets, Lambda API protocol.
- **runtime/rust-binding/**: `no_std` Rust FFI wrapper around the C runtime.
- **runtime/shim/**: `no_std`, `no_main` crate. Provides `_start()`, links handler.o, bridges event loop.
- **scripts/**: build.py, deploy.py (boto3), test.py.

## Handler Convention

```python
class Handler:
    def init(self) -> None:        # cold-start (called once)
        pass
    def handle(self, event_ptr: __ptr__, event_len: int,
               response_ptr: __ptr__, response_cap: int) -> int:
        return 0                   # returns response length
```

## Key Decisions

- TLS: dynamic-link OpenSSL from AL2023 (zero binary cost)
- SigV4: pure Rust SHA-256 (no OpenSSL for crypto)
- Compiler stays in Python (no self-hosting)
- Cross-compile target: x86_64-unknown-linux-gnu
- Deploy via boto3, not awscli

## Specialist Domains

When working on this project, these are the 5 specialist areas:

1. **Compiler/LLVM IR** — compiler.py, builtins.pie, type system, AST visitors, codegen
2. **Rust Runtime** — runtime/rust-binding/, runtime/shim/, no_std FFI, Writer API
3. **C Runtime** — runtime/src/, HTTP protocol, socket handling, Lambda API
4. **AWS Integration** — scripts/deploy.py, SigV4, Smithy codegen, boto3 shims
5. **Build Toolchain** — scripts/build.py, cross-compilation, llc, cargo, Docker
