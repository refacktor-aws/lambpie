import pytest
import sys
import os
import ast
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from compiler import Compiler
from llvmlite import ir, binding


def compile_lambpie_code(code_str):
    builtin_path = os.path.join(os.path.dirname(__file__), '..', 'builtins.pie')
    builtin_path = os.path.abspath(builtin_path)

    with open(builtin_path, 'r') as f:
        builtin_code = f.read()
    builtin_ast = ast.parse(builtin_code, filename=builtin_path)

    test_ast = ast.parse(code_str, filename="<test_code>")

    combined_ast = ast.Module(
        body=builtin_ast.body + test_ast.body,
        type_ignores=[]
    )

    unique_module_name = f"lambpie_test_{uuid.uuid4().hex}"
    compiler = Compiler(module_name=unique_module_name)
    llvm_ir = compiler.compile(combined_ast)
    return str(llvm_ir)


def test_echo_handler():
    code = """
from C import memcpy

class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        memcpy(response_ptr, event_ptr, event_len)
        return event_len

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)

    # Verify lambpie_init and lambpie_handle are emitted (llvmlite quotes names)
    assert 'define void @"lambpie_init"()' in llvm_ir, "lambpie_init not found"
    assert 'define i64 @"lambpie_handle"(' in llvm_ir, "lambpie_handle not found"

    # Verify Handler methods exist
    assert "Handler_init" in llvm_ir, "Handler_init method not found"
    assert "Handler_handle" in llvm_ir, "Handler_handle method not found"

    # Verify global handler pointer
    assert '@"lambpie_handler"' in llvm_ir, "global handler pointer not found"

    # Verify arena allocator is used (not malloc) for Handler
    assert "lambpie_arena_alloc" in llvm_ir, "arena allocator not found"

    print("\n--- Test: test_echo_handler ---")
    print(llvm_ir)


def test_simple_if():
    code = """
class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        if event_len > 0:
            return event_len
        return 0

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    assert "if.then" in llvm_ir
    assert "if.end" in llvm_ir
    assert "br i1" in llvm_ir
    print("\n--- Test: test_simple_if ---")
    print(llvm_ir)


def test_handler_with_loop():
    code = """
class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        i: int = 0
        while i < event_len:
            i = i + 1
        return i

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    assert "loop.header" in llvm_ir
    assert "loop.body" in llvm_ir
    assert "loop.exit" in llvm_ir
    print("\n--- Test: test_handler_with_loop ---")
    print(llvm_ir)


def test_arena_static_tag_in_init():
    """Handler allocated in lambpie_init uses static arena (tag 0)."""
    code = """
class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        return 0

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    # lambpie_init should use arena tag 0 (STATIC)
    assert 'call i8* @"lambpie_arena_alloc"(i32 0,' in llvm_ir, \
        "lambpie_init should allocate Handler on static arena (tag 0)"


def test_arena_req_tag_in_handle():
    """Objects constructed inside handle() use request arena (tag 1)."""
    code = """
class Temp:
    val: int

    def __init__(self, v: int) -> None:
        pass

class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        t: Temp = Temp(42)
        return 0

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    # Inside Handler_handle, Temp() should use arena tag 1 (REQ)
    # Find the Handler_handle function and check for tag 1
    handle_start = llvm_ir.index('@"Handler_handle"')
    handle_ir = llvm_ir[handle_start:]
    assert 'call i8* @"lambpie_arena_alloc"(i32 1,' in handle_ir, \
        "Handler.handle should allocate Temp on request arena (tag 1)"


def test_arena_static_tag_in_handler_init():
    """Objects constructed inside Handler.init() use static arena (tag 0)."""
    code = """
class Config:
    val: int

    def __init__(self, v: int) -> None:
        pass

class Handler:
    def init(self) -> None:
        c: Config = Config(99)

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        return 0

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    # Inside Handler_init, Config() should use arena tag 0 (STATIC)
    init_start = llvm_ir.index('@"Handler_init"')
    init_end = llvm_ir.index('@"Handler_handle"')
    init_ir = llvm_ir[init_start:init_end]
    assert 'call i8* @"lambpie_arena_alloc"(i32 0,' in init_ir, \
        "Handler.init should allocate Config on static arena (tag 0)"


def test_target_triple():
    """Default target triple should be x86_64-unknown-linux-gnu."""
    code = """
class Handler:
    def init(self) -> None:
        pass

    def handle(self, event_ptr: __ptr__, event_len: int, response_ptr: __ptr__, response_cap: int) -> int:
        return 0

if __name__ == '__main__':
    app: Handler = Handler()
"""
    llvm_ir = compile_lambpie_code(code)
    assert 'target triple = "x86_64-unknown-linux-gnu"' in llvm_ir, \
        "Default target should be Lambda triple"
