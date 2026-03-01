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


# ---------------------------------------------------------------------------
# Item 5: SHA-256 prerequisites — bitwise operations
# ---------------------------------------------------------------------------

def _int_op_handle(body_expr):
    """Helper: wrap a single-int-field Request/Response around a body expression."""
    return f"""
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = {body_expr}
    return Response(result)
"""


def test_bitwise_and():
    """BinOp & emits 'and i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x & 255"))
    assert "and i64" in llvm_ir, "Bitwise AND should emit 'and i64'"


def test_bitwise_or():
    """BinOp | emits 'or i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x | 1"))
    assert "or i64" in llvm_ir, "Bitwise OR should emit 'or i64'"


def test_bitwise_xor():
    """BinOp ^ emits 'xor i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x ^ 42"))
    assert "xor i64" in llvm_ir, "Bitwise XOR should emit 'xor i64'"


def test_bitwise_not():
    """UnaryOp ~ emits 'xor i64 ..., -1' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("~event.x"))
    # Bitwise NOT is lowered as XOR with -1
    assert "xor i64" in llvm_ir, "Bitwise NOT should emit 'xor i64'"
    assert "-1" in llvm_ir, "Bitwise NOT mask should be -1 (all ones)"


def test_left_shift():
    """BinOp << emits 'shl i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x << 3"))
    assert "shl i64" in llvm_ir, "Left shift should emit 'shl i64'"


def test_right_shift():
    """BinOp >> emits 'lshr i64' (logical shift right) in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x >> 2"))
    assert "lshr i64" in llvm_ir, "Right shift should emit 'lshr i64'"


def test_modulo():
    """BinOp % emits 'srem i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x % 7"))
    assert "srem i64" in llvm_ir, "Modulo should emit 'srem i64'"


def test_subtraction():
    """BinOp - emits 'sub i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x - 1"))
    assert "sub i64" in llvm_ir, "Subtraction should emit 'sub i64'"


def test_multiplication():
    """BinOp * emits 'mul i64' in IR."""
    llvm_ir, _ = compile_lambpie_code(_int_op_handle("event.x * 3"))
    assert "mul i64" in llvm_ir, "Multiplication should emit 'mul i64'"


def test_compare_lt():
    """Compare < emits 'icmp slt' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x < 10:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp slt" in llvm_ir, "Less-than compare should emit 'icmp slt'"


def test_compare_gt():
    """Compare > emits 'icmp sgt' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x > 10:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp sgt" in llvm_ir, "Greater-than compare should emit 'icmp sgt'"


def test_compare_lte():
    """Compare <= emits 'icmp sle' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x <= 10:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp sle" in llvm_ir, "Less-than-or-equal compare should emit 'icmp sle'"


def test_compare_gte():
    """Compare >= emits 'icmp sge' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x >= 10:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp sge" in llvm_ir, "Greater-than-or-equal compare should emit 'icmp sge'"


def test_compare_eq():
    """Compare == emits 'icmp eq' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x == 0:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp eq" in llvm_ir, "Equality compare should emit 'icmp eq'"


def test_compare_ne():
    """Compare != emits 'icmp ne' in IR."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = 0
    if event.x != 0:
        result = 1
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "icmp ne" in llvm_ir, "Not-equal compare should emit 'icmp ne'"


def test_while_loop():
    """While loop emits loop.header / loop.body / loop.exit blocks."""
    code = """
class Request:
    n: int

class Response:
    result: int

def handle(event: Request) -> Response:
    i: int = 0
    s: int = 0
    while i < event.n:
        s = s + i
        i = i + 1
    return Response(s)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "loop.header" in llvm_ir, "While loop should emit loop.header block"
    assert "loop.body" in llvm_ir, "While loop should emit loop.body block"
    assert "loop.exit" in llvm_ir, "While loop should emit loop.exit block"


def test_while_loop_with_bitwise():
    """While loop body can contain bitwise operations (foundation for SHA-256 rounds)."""
    code = """
class Request:
    n: int

class Response:
    result: int

def handle(event: Request) -> Response:
    i: int = 0
    acc: int = 0
    while i < 32:
        acc = acc ^ (event.n >> i)
        acc = acc & 4294967295
        i = i + 1
    return Response(acc)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "loop.header" in llvm_ir
    assert "xor i64" in llvm_ir
    assert "lshr i64" in llvm_ir
    assert "and i64" in llvm_ir


def test_array_declaration():
    """array[int, N] declares a fixed-size stack array of [N x i64]."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    buf: array[int, 16]
    buf[0] = event.x
    result: int = buf[0]
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "[16 x i64]" in llvm_ir, "array[int, 16] should produce [16 x i64] type"


def test_array_store_and_load():
    """Array element store (arr[i] = val) and load (arr[i]) round-trip."""
    code = """
class Request:
    a: int
    b: int

class Response:
    result: int

def handle(event: Request) -> Response:
    scratch: array[int, 4]
    scratch[0] = event.a
    scratch[1] = event.b
    scratch[2] = scratch[0] + scratch[1]
    result: int = scratch[2]
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    # Should have stores and loads into [4 x i64]
    assert "[4 x i64]" in llvm_ir, "array[int, 4] should produce [4 x i64]"
    assert "store i64" in llvm_ir, "Array element assignment should emit store"


def test_array_with_while_loop():
    """Array + while loop: fill array elements via loop index."""
    code = """
class Request:
    seed: int

class Response:
    result: int

def handle(event: Request) -> Response:
    w: array[int, 8]
    i: int = 0
    while i < 8:
        w[i] = event.seed ^ i
        i = i + 1
    result: int = w[0] ^ w[7]
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "[8 x i64]" in llvm_ir
    assert "loop.header" in llvm_ir
    assert "xor i64" in llvm_ir


def test_local_variable_assignment():
    """Plain assignment to declared local variable (without type annotation)."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    v: int = 0
    v = event.x + 10
    return Response(v)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "store i64" in llvm_ir, "Assignment should emit store"


def test_local_variable_assign_undeclared_raises():
    """Assigning to an undeclared variable raises NameError."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    undeclared = event.x + 1
    return Response(undeclared)
"""
    with pytest.raises(NameError, match="Cannot assign to undeclared variable"):
        compile_lambpie_code(code)


def test_unsupported_binop_raises():
    """Unsupported binary operator raises NotImplementedError, not a silent fallback."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    result: int = event.x // 2
    return Response(result)
"""
    with pytest.raises(NotImplementedError, match="Unsupported binary operator"):
        compile_lambpie_code(code)


def test_combined_bitwise_expression():
    """Complex nested bitwise expression emits correct IR instructions."""
    code = """
class Request:
    x: int

class Response:
    result: int

def handle(event: Request) -> Response:
    a: int = (event.x << 3) | (event.x >> 29)
    b: int = a ^ 2654435769
    c: int = ~b & 4294967295
    result: int = c % 256
    return Response(result)
"""
    llvm_ir, _ = compile_lambpie_code(code)
    assert "shl i64" in llvm_ir
    assert "lshr i64" in llvm_ir
    assert "or i64" in llvm_ir
    assert "xor i64" in llvm_ir
    assert "and i64" in llvm_ir
    assert "srem i64" in llvm_ir
