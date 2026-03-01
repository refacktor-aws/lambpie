# lambpie

A minimalist language for coding highly-efficient AWS Lambda functions. It features AWS SDK as built-in language primitives (hence, no library bloat) and a strict dual-arena (static & request-time) memory management system. It is intended to be safe and highly efficient for solving simple problems, and its syntax provides an easy exit hatch to Python when complexity grows.

Valid `.pie` is valid Python 3. Rename to `.py` and it runs with boto3.

## Example

```python
# echo.pie — echoes the Lambda event back as the response
from C import memcpy

class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int,
               response_ptr: __ptr__, response_cap: int) -> int:
        memcpy(response_ptr, event_ptr, event_len)
        return event_len

if __name__ == '__main__':
    app: Handler = Handler()
```

## How it works

```
handler.pie → compiler.py → handler.ll → handler.o → Rust shim → bootstrap
```

1. **compiler.py** parses `.pie` (Python AST) and emits LLVM IR via `llvmlite`
2. `llc` compiles IR to a relocatable object file
3. A `no_std` Rust shim links the object file with a minimal C runtime
4. Output is a single static binary (`bootstrap`) for AWS Lambda `provided.al2023`

The compiler emits two `extern "C"` entry points:
- `lambpie_init()` — called once at cold start (static arena)
- `lambpie_handle(event_ptr, event_len, response_ptr, response_cap) -> response_len` — called per request (request arena)

## Memory model

Two bump-allocated arenas. No malloc, no free, no GC.

| Arena | Tag | Lifetime | Used in |
|-------|-----|----------|---------|
| Static | 0 | Process lifetime | `__init__()`, `init()` |
| Request | 1 | Single invocation | `handle()` |

- Static arena is frozen (mprotect read-only) after `init()` completes
- Request arena is bulk-reset after each invocation (pointer bump back to base)
- Writes to frozen static arena cause SIGSEGV — no dangling pointers possible

## Build & test

```bash
# Run compiler unit tests
python -m pytest tests/test_compiler.py -v

# Compile .pie to LLVM IR
python compiler.py tests/echo.pie -o target/echo

# Full pipeline: .pie → bootstrap binary (requires Linux or Docker)
python scripts/build.py tests/echo.pie

# Deploy to AWS Lambda
python scripts/deploy.py --function-name echo \
    --build tests/echo.pie \
    --role arn:aws:iam::123456789012:role/lambda-role \
    --memory 128 --timeout 10
```

## Project structure

```
lambpie/
├── compiler.py           # Python AST → LLVM IR compiler
├── builtins.pie          # Built-in type stubs (str, bytearray)
├── runtime/
│   ├── src/
│   │   ├── arena.c       # Dual-arena bump allocator
│   │   ├── arena.h
│   │   ├── runtime.c     # Lambda Runtime API (HTTP/TCP)
│   │   └── runtime.h
│   ├── rust-binding/     # no_std Rust FFI wrapper
│   └── shim/             # no_std, no_main binary crate (_start)
├── scripts/
│   ├── build.py          # .pie → bootstrap pipeline
│   ├── deploy.py         # boto3 deployment
│   └── test.py           # Build + mock Lambda test
├── tests/
│   ├── echo.pie          # Echo handler example
│   ├── test_compiler.py  # Compiler unit tests
│   └── test_runtime.py   # Mock Lambda Runtime API
└── .claude/agents/       # Specialist subagent definitions
```

## Roadmap

- [x] M1: Lambda echo handler (compile, link, deploy)
- [x] M2: Dual-arena bump allocator with arena tags
- [ ] M3: Typed structs and JSON serialization
- [ ] M4: Raw HTTP + SigV4 signing (implemented in .pie)
- [ ] M5: SDK modules from botocore JSON models
- [ ] M6: Python compatibility package (`pip install lambpie-aws`)
