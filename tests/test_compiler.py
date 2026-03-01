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
    return str(llvm_ir), compiler


def test_typed_echo_handler():
    """Full typed echo handler with auto __init__, field access, JSON helpers."""
    code = """
class Request:
    message: str
    number: int

class Response:
    status: str
    echo: str
    doubled: int

def handle(event: Request) -> Response:
    return Response("ok", event.message, event.number + event.number)
"""
    llvm_ir, compiler = compile_lambpie_code(code)

    # Verify lambpie_init and lambpie_handle are emitted
    assert 'define void @"lambpie_init"()' in llvm_ir, "lambpie_init not found"
    assert 'define i64 @"lambpie_handle"(' in llvm_ir, "lambpie_handle not found"

    # Verify user handle function exists
    assert '@"handle"(' in llvm_ir, "handle function not found"

    # Verify auto-generated __init__ for Request and Response
    assert 'Request___init__' in llvm_ir, "Request auto __init__ not found"
    assert 'Response___init__' in llvm_ir, "Response auto __init__ not found"

    # Verify JSON helper declarations
    assert 'json_get_str' in llvm_ir, "json_get_str not declared"
    assert 'json_get_int' in llvm_ir, "json_get_int not declared"
    assert 'json_open' in llvm_ir, "json_open not declared"
    assert 'json_write_str' in llvm_ir, "json_write_str not declared"
    assert 'json_write_int' in llvm_ir, "json_write_int not declared"
    assert 'json_close' in llvm_ir, "json_close not declared"

    # Verify no Handler class references
    assert 'Handler' not in llvm_ir, "Old Handler class should not exist"

    # Verify arena allocator is used
    assert 'lambpie_arena_alloc' in llvm_ir, "arena allocator not found"

    # Verify metadata
    meta = compiler.get_metadata()
    assert meta['trigger'] == 'direct'
    assert meta['event_type'] == 'Request'
    assert meta['response_type'] == 'Response'
    assert meta['event_fields'] == {'message': 'str', 'number': 'int'}
    assert meta['response_fields'] == {'status': 'str', 'echo': 'str', 'doubled': 'int'}


def test_arena_req_in_handle():
    """lambpie_handle uses request arena (tag 1) for event deserialization."""
    code = """
class Request:
    message: str
    number: int

class Response:
    echo: str

def handle(event: Request) -> Response:
    return Response(event.message)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    # lambpie_handle should use arena tag 1 (REQ) for event struct allocation
    handle_start = llvm_ir.index('@"lambpie_handle"')
    handle_ir = llvm_ir[handle_start:]
    assert 'call i8* @"lambpie_arena_alloc"(i32 1,' in handle_ir, \
        "lambpie_handle should allocate on request arena (tag 1)"


def test_arena_static_in_init():
    """lambpie_init uses static arena (tag 0)."""
    code = """
class Config:
    val: int

class Request:
    number: int

class Response:
    number: int

c: Config = Config(99)

def handle(event: Request) -> Response:
    return Response(event.number)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    # lambpie_init should use arena tag 0 (STATIC) for Config allocation
    init_start = llvm_ir.index('@"lambpie_init"')
    init_end = llvm_ir.index('@"lambpie_handle"')
    init_ir = llvm_ir[init_start:init_end]
    assert 'call i8* @"lambpie_arena_alloc"(i32 0,' in init_ir, \
        "lambpie_init should allocate on static arena (tag 0)"


def test_field_access():
    """GEP + load for struct field access."""
    code = """
class Request:
    number: int

class Response:
    number: int

def handle(event: Request) -> Response:
    return Response(event.number)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    # Field access should use GEP (getelementptr)
    assert 'getelementptr' in llvm_ir, "Field access should use GEP"
    # The handle function should load from event struct
    assert '@"handle"(' in llvm_ir, "handle function not found"


def test_missing_handle_raises():
    """RuntimeError when no handle() function is defined."""
    code = """
class Request:
    number: int
"""
    with pytest.raises(RuntimeError, match="No handle\\(\\) function found"):
        compile_lambpie_code(code)


def test_target_triple():
    """Default target triple should be x86_64-unknown-linux-gnu."""
    code = """
class Request:
    number: int

class Response:
    number: int

def handle(event: Request) -> Response:
    return Response(event.number)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert 'target triple = "x86_64-unknown-linux-gnu"' in llvm_ir, \
        "Default target should be Lambda triple"
