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

    # Verify malloc is called in init (for Handler allocation)
    assert "malloc" in llvm_ir, "malloc not found"

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
