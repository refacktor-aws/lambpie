import ast
import json
import sys
import os
from llvmlite import ir, binding

class Compiler(ast.NodeVisitor):
    LAMBDA_TRIPLE = 'x86_64-unknown-linux-gnu'
    LAMBDA_DATA_LAYOUT = 'e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128'

    def __init__(self, module_name="lambpie_module", target_triple=None):
        # LLVM setup
        self.binding = binding
        self.binding.initialize()
        self.binding.initialize_native_target()
        self.binding.initialize_native_asmprinter()

        self.context = ir.Context()
        self.module = ir.Module(name=module_name, context=self.context)

        # Default to Lambda target triple, override with explicit arg
        triple = target_triple or self.LAMBDA_TRIPLE
        self.module.triple = triple
        if triple == self.LAMBDA_TRIPLE:
            self.module.data_layout = self.LAMBDA_DATA_LAYOUT
        else:
            target = self.binding.Target.from_triple(triple)
            target_machine = target.create_target_machine()
            self.module.data_layout = target_machine.target_data

        # Type definitions
        self.types = {
            'int': ir.IntType(64),
            'float': ir.DoubleType(),
            'None': ir.VoidType(),
            '__ptr__': ir.IntType(8).as_pointer(),
        }

        # Symbol tables
        self.global_scope = {}
        self.local_scope = {}
        self.class_layouts = {}
        self.builder = None

        # Initialize __name__ as a global constant
        main_str_val = "__main__\0"
        main_c_string = ir.Constant(ir.ArrayType(ir.IntType(8), len(main_str_val)),
                                    bytearray(main_str_val, 'utf8'))

        main_global_var = ir.GlobalVariable(self.module, main_c_string.type, name=".str.__main__")
        main_global_var.linkage = 'private'
        main_global_var.initializer = main_c_string

        self.global_scope['__name__'] = main_global_var.gep([ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), 0)])

        # Pre-declare malloc (kept for builtins compatibility)
        malloc_type = ir.FunctionType(ir.IntType(8).as_pointer(), [ir.IntType(64)])
        self.global_scope['malloc'] = ir.Function(self.module, malloc_type, name='malloc')

        # Pre-declare memcpy for bytes copy operations
        memcpy_type = ir.FunctionType(ir.IntType(8).as_pointer(),
                                      [ir.IntType(8).as_pointer(), ir.IntType(8).as_pointer(), ir.IntType(64)])
        if 'memcpy' not in self.global_scope:
            self.global_scope['memcpy'] = ir.Function(self.module, memcpy_type, name='memcpy')

        # Arena allocator: lambpie_arena_alloc(tag: i32, size: i64) -> i8*
        arena_alloc_type = ir.FunctionType(
            ir.IntType(8).as_pointer(),
            [ir.IntType(32), ir.IntType(64)])
        self.global_scope['lambpie_arena_alloc'] = ir.Function(
            self.module, arena_alloc_type, name='lambpie_arena_alloc')

        # Arena tag constants
        self.ARENA_STATIC = 0
        self.ARENA_REQ = 1

        # Current arena context: which arena to allocate from
        # Set to STATIC during init(), REQ during handle()
        self.current_arena = self.ARENA_STATIC

        # Arena tags per variable: tracks which arena a value was allocated from
        # Maps variable name -> arena tag (ARENA_STATIC or ARENA_REQ)
        self.arena_tags = {}

        # Monotonic counter for collision-free global names
        self._name_counter = 0

        # Flat module model: deferred handle function and init statements
        self._handle_node = None
        self._init_stmts = []

    def _get_type(self, annotation_node):
        if isinstance(annotation_node, ast.Name):
            type_name = annotation_node.id
            if type_name == 'bytes':
                return self.types['__ptr__']
            if type_name in self.types:
                return self.types[type_name]
            # Check if it's a known class name (registered in class_layouts)
            if type_name in self.class_layouts:
                # Register pointer type lazily
                struct_type = self.module.context.get_identified_type(type_name)
                self.types[type_name] = struct_type.as_pointer()
                return self.types[type_name]
            raise TypeError(f"Unknown type: {type_name}")

        elif isinstance(annotation_node, ast.Constant) and annotation_node.value is None:
            return self.types['None']

        elif isinstance(annotation_node, ast.Subscript):
            if not isinstance(annotation_node.value, ast.Name) or annotation_node.value.id != 'ptr':
                raise TypeError("Invalid pointer syntax, expected 'ptr[...]'")
            inner_type = self._get_type(annotation_node.slice)
            return ir.PointerType(inner_type)

        elif isinstance(annotation_node, ast.Index):
             return self._get_type(annotation_node.value)

        else:
            raise TypeError(f"Unsupported type annotation: {ast.dump(annotation_node)}")

    def visit_Module(self, node):
        for sub_node in node.body:
            # Skip if __name__ == '__main__' blocks
            is_main_block = (
                isinstance(sub_node, ast.If) and
                isinstance(sub_node.test, ast.Compare) and
                len(sub_node.test.ops) == 1 and
                isinstance(sub_node.test.ops[0], ast.Eq) and
                isinstance(sub_node.test.left, ast.Name) and
                sub_node.test.left.id == '__name__' and
                len(sub_node.test.comparators) == 1 and
                isinstance(sub_node.test.comparators[0], ast.Constant) and
                sub_node.test.comparators[0].value == '__main__'
            )
            if is_main_block:
                continue

            # ClassDef, ImportFrom → visit immediately (type registration)
            if isinstance(sub_node, (ast.ClassDef, ast.ImportFrom)):
                self.visit(sub_node)
            # Top-level handle() → defer compilation
            elif isinstance(sub_node, ast.FunctionDef) and sub_node.name == 'handle':
                self._handle_node = sub_node
            # Other FunctionDef → visit immediately
            elif isinstance(sub_node, ast.FunctionDef):
                self.visit(sub_node)
            # Top-level statements → collect for lambpie_init
            elif isinstance(sub_node, (ast.AnnAssign, ast.Assign, ast.Expr)):
                self._init_stmts.append(sub_node)
            else:
                self.visit(sub_node)

    def visit_ImportFrom(self, node):
        if node.module == 'C':
            for alias in node.names:
                name = alias.name
                if name in self.global_scope:
                    continue  # already declared (e.g. memcpy pre-declared in __init__)

                C_SIGNATURES = {
                    'printf': ir.FunctionType(ir.IntType(32), [self.types['__ptr__']], var_arg=True),
                    'atoi': ir.FunctionType(self.types['int'], [self.types['__ptr__']]),
                    'strlen': ir.FunctionType(self.types['int'], [self.types['__ptr__']]),
                    'free': ir.FunctionType(ir.VoidType(), [self.types['__ptr__']]),
                }
                if name not in C_SIGNATURES:
                    raise TypeError(f"Unknown C function signature: {name}. Add it to C_SIGNATURES in visit_ImportFrom.")
                func_type = C_SIGNATURES[name]

                self.global_scope[name] = ir.Function(self.module, func_type, name=name)
        else:
            raise ImportError(f"Unknown module: {node.module}. Only 'from C import ...' is supported.")

    def visit_ClassDef(self, node):
        class_name = node.name
        class_type = self.module.context.get_identified_type(class_name)

        ref_count_type = ir.IntType(32)
        attributes = {'__ref_count__': (0, ref_count_type)}
        attribute_types = [ref_count_type]

        attr_index = 1
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                attr_name = stmt.target.id
                attr_type = self._get_type(stmt.annotation)
                attributes[attr_name] = (attr_index, attr_type)
                attribute_types.append(attr_type)
                attr_index += 1

        class_type.set_body(*attribute_types)

        self.types[class_name] = class_type.as_pointer()
        self.class_layouts[class_name] = attributes

        # Check if class has an explicit __init__
        has_init = any(
            isinstance(stmt, ast.FunctionDef) and stmt.name == '__init__'
            for stmt in node.body
        )

        # Auto-generate __init__ if none exists
        if not has_init:
            self._generate_auto_init(class_name, class_type, attributes)

        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef):
                self.visit_FunctionDef(stmt, class_name=class_name)

    def _is_str_type(self, llvm_type):
        """Check if an LLVM type is a pointer to the str struct."""
        if not isinstance(llvm_type, ir.PointerType):
            return False
        if not isinstance(llvm_type.pointee, ir.IdentifiedStructType):
            return False
        return llvm_type.pointee.name == 'str'

    def _wrap_cstr_to_str(self, i8_ptr):
        """Wrap an i8* (C string literal) into a %str* struct on current arena."""
        str_ptr_type = self.types['str']

        # Compute size of str struct
        null_ptr = ir.Constant(str_ptr_type, None)
        size_ptr = self.builder.gep(null_ptr, [ir.Constant(ir.IntType(32), 1)], inbounds=False)
        size = self.builder.ptrtoint(size_ptr, self.types['int'], name='str.size')

        # Allocate on current arena
        arena_alloc = self.global_scope['lambpie_arena_alloc']
        tag = ir.Constant(ir.IntType(32), self.current_arena)
        raw = self.builder.call(arena_alloc, [tag, size], 'str.raw')
        str_ptr = self.builder.bitcast(raw, str_ptr_type, 'str.ptr')

        # Get str struct layout: {i32 ref_count, i64 len, i8* buffer}
        layout = self.class_layouts['str']

        # Set ref_count = 1
        ref_idx = layout['__ref_count__'][0]
        ref_ptr = self.builder.gep(str_ptr,
            [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), ref_idx)],
            inbounds=True, name='str.rc.ptr')
        self.builder.store(ir.Constant(ir.IntType(32), 1), ref_ptr)

        # Compute strlen
        strlen_fn = self.global_scope['strlen']
        length = self.builder.call(strlen_fn, [i8_ptr], 'str.len')

        # Set len field
        len_idx = layout['len'][0]
        len_ptr = self.builder.gep(str_ptr,
            [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), len_idx)],
            inbounds=True, name='str.len.ptr')
        self.builder.store(length, len_ptr)

        # Set buffer field
        buf_idx = layout['buffer'][0]
        buf_ptr = self.builder.gep(str_ptr,
            [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), buf_idx)],
            inbounds=True, name='str.buf.ptr')
        self.builder.store(i8_ptr, buf_ptr)

        return str_ptr

    def _coerce_arg(self, value, expected_type):
        """Coerce a value to the expected type if needed (e.g., i8* -> %str*)."""
        if self._is_str_type(expected_type):
            # Check if value is i8* (string literal pointer)
            if isinstance(value.type, ir.PointerType) and isinstance(value.type.pointee, ir.IntType) and value.type.pointee.width == 8:
                return self._wrap_cstr_to_str(value)
        return value

    def _generate_auto_init(self, class_name, class_type, attributes):
        """Generate __init__(self, field1, field2, ...) for a class without explicit __init__."""
        # Collect fields in order (skip __ref_count__)
        fields = [(name, idx, typ) for name, (idx, typ) in attributes.items()
                  if name != '__ref_count__']
        fields.sort(key=lambda f: f[1])  # sort by field index

        # Build function type: (self_ptr, field1_type, field2_type, ...) -> void
        self_ptr_type = class_type.as_pointer()
        arg_types = [self_ptr_type] + [typ for _, _, typ in fields]
        func_type = ir.FunctionType(ir.VoidType(), arg_types)

        func_name = f"{class_name}___init__"
        llvm_func = ir.Function(self.module, func_type, name=func_name)
        self.global_scope[func_name] = llvm_func

        # Save/restore builder context
        saved_builder = self.builder
        saved_local = self.local_scope

        entry = llvm_func.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.local_scope = {}

        # Name args and store self
        llvm_func.args[0].name = 'self'
        self_alloc = self.builder.alloca(self_ptr_type, name='self')
        self.builder.store(llvm_func.args[0], self_alloc)
        self_ptr = self.builder.load(self_alloc, name='self.val')

        # Set __ref_count__ = 1
        ref_idx = attributes['__ref_count__'][0]
        ref_ptr = self.builder.gep(self_ptr,
            [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), ref_idx)],
            inbounds=True, name='ref_count.ptr')
        self.builder.store(ir.Constant(ir.IntType(32), 1), ref_ptr)

        # Store each field from the corresponding arg
        for i, (field_name, field_idx, field_type) in enumerate(fields):
            arg = llvm_func.args[i + 1]  # +1 for self
            arg.name = field_name
            field_ptr = self.builder.gep(self_ptr,
                [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), field_idx)],
                inbounds=True, name=f'{field_name}.ptr')
            self.builder.store(arg, field_ptr)

        self.builder.ret_void()

        # Restore context
        self.builder = saved_builder
        self.local_scope = saved_local

    def visit_FunctionDef(self, node, class_name=None):
        func_name = node.name
        if class_name:
            func_name = f"{class_name}_{func_name}"

        # Set arena context
        saved_arena = self.current_arena
        if not class_name and node.name == 'handle':
            # Top-level handle() uses request arena
            self.current_arena = self.ARENA_REQ

        return_type = self._get_type(node.returns)

        arg_types = []
        if class_name:
            arg_types.append(self.types[class_name])

        for arg in node.args.args:
            if arg.arg == 'self':
                continue
            arg_types.append(self._get_type(arg.annotation))

        func_type = ir.FunctionType(return_type, arg_types)

        llvm_func = ir.Function(self.module, func_type, name=func_name)
        self.global_scope[func_name] = llvm_func

        entry_block = llvm_func.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry_block)

        self.local_scope = {}
        self.arena_tags = {}

        ast_args = [arg for arg in node.args.args if arg.arg != 'self']

        for i, llvm_arg in enumerate(llvm_func.args):
            if class_name and i == 0:
                arg_name = 'self'
            else:
                ast_arg_index = i - 1 if class_name else i
                arg_name = ast_args[ast_arg_index].arg

            llvm_arg.name = arg_name
            arg_alloc = self.builder.alloca(llvm_arg.type, name=arg_name)
            self.builder.store(llvm_arg, arg_alloc)
            self.local_scope[arg_name] = arg_alloc

        for stmt in node.body:
            self.visit(stmt)

        if not self.builder.block.is_terminated:
            if return_type != ir.VoidType():
                self.builder.unreachable()
            else:
                self.builder.ret_void()

        # Restore arena context
        self.current_arena = saved_arena

    def visit_Return(self, node):
        if node.value:
            value = self.visit(node.value)
            self.builder.ret(value)
        else:
            self.builder.ret_void()

    def visit_Constant(self, node):
        if isinstance(node.value, int):
            return ir.Constant(self.types['int'], node.value)
        elif isinstance(node.value, str):
            string_val = node.value + '\0'
            c_string = ir.Constant(ir.ArrayType(ir.IntType(8), len(string_val)),
                                   bytearray(string_val, 'utf8'))

            self._name_counter += 1
            global_var = ir.GlobalVariable(self.module, c_string.type, name=f".str.{self._name_counter}")
            global_var.linkage = 'private'
            global_var.initializer = c_string

            return global_var.gep([ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), 0)])
        elif isinstance(node.value, bytes):
            # Handle bytes literals like b'{"status": "ok"}'
            byte_vals = list(node.value)
            c_bytes = ir.Constant(ir.ArrayType(ir.IntType(8), len(byte_vals)),
                                  byte_vals)

            self._name_counter += 1
            global_var = ir.GlobalVariable(self.module, c_bytes.type,
                                           name=f".bytes.{self._name_counter}")
            global_var.linkage = 'private'
            global_var.initializer = c_bytes
            global_var.global_constant = True

            return global_var.gep([ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), 0)])

    def visit_Name(self, node):
        if node.id in self.local_scope:
            return self.builder.load(self.local_scope[node.id], name=node.id)
        elif node.id in self.global_scope:
            return self.global_scope[node.id]
        elif node.id in self.types:
            return self.types[node.id]
        else:
            raise NameError(f"Name not found in any scope: {node.id}")

    def visit_BinOp(self, node):
        lhs = self.visit(node.left)
        rhs = self.visit(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return self.builder.add(lhs, rhs, name='addtmp')
        elif isinstance(op, ast.Sub):
            return self.builder.sub(lhs, rhs, name='subtmp')
        else:
            raise NotImplementedError(f"Unsupported binary operator: {type(op).__name__}")

    def visit_Compare(self, node):
        lhs = self.visit(node.left)
        if len(node.ops) != 1:
            raise NotImplementedError("Chained comparisons are not supported")

        op = node.ops[0]
        rhs = self.visit(node.comparators[0])

        if isinstance(op, ast.Eq):
            return self.builder.icmp_signed('==', lhs, rhs, name='eqtmp')
        elif isinstance(op, ast.NotEq):
            return self.builder.icmp_signed('!=', lhs, rhs, name='netmp')
        elif isinstance(op, ast.Lt):
            return self.builder.icmp_signed('<', lhs, rhs, name='lttmp')
        elif isinstance(op, ast.LtE):
            return self.builder.icmp_signed('<=', lhs, rhs, name='letmp')
        elif isinstance(op, ast.Gt):
            return self.builder.icmp_signed('>', lhs, rhs, name='gttmp')
        elif isinstance(op, ast.GtE):
            return self.builder.icmp_signed('>=', lhs, rhs, name='getmp')
        else:
            raise NotImplementedError(f"Unsupported comparison operator: {type(op).__name__}")

    def visit_If(self, node):
        func = self.builder.function

        if_entry_block = func.append_basic_block(name='if.entry')
        then_block = func.append_basic_block(name='if.then')

        if node.orelse:
            else_block = func.append_basic_block(name='if.else')
        else:
            else_block = None

        merge_block = func.append_basic_block(name='if.end')

        self.builder.branch(if_entry_block)
        self.builder.position_at_end(if_entry_block)

        condition = self.visit(node.test)
        if else_block:
            self.builder.cbranch(condition, then_block, else_block)
        else:
            self.builder.cbranch(condition, then_block, merge_block)

        self.builder.position_at_end(then_block)
        for stmt in node.body:
            self.visit(stmt)
        if not self.builder.block.is_terminated:
            self.builder.branch(merge_block)

        if else_block:
            self.builder.position_at_end(else_block)
            for stmt in node.orelse:
                self.visit(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(merge_block)

        self.builder.position_at_end(merge_block)

    def visit_While(self, node):
        func = self.builder.function
        header_block = func.append_basic_block(name='loop.header')
        body_block = func.append_basic_block(name='loop.body')
        exit_block = func.append_basic_block(name='loop.exit')

        self.builder.branch(header_block)
        self.builder.position_at_end(header_block)

        condition = self.visit(node.test)
        self.builder.cbranch(condition, body_block, exit_block)

        self.builder.position_at_end(body_block)
        for stmt in node.body:
            self.visit(stmt)

        if not self.builder.block.is_terminated:
            self.builder.branch(header_block)

        self.builder.position_at_end(exit_block)

    def visit_Attribute(self, node):
        obj_ptr = self.visit(node.value)
        obj_type_name = obj_ptr.type.pointee.name

        attr_name = node.attr
        mangled_name = f"{obj_type_name}_{attr_name}"

        # 1. Try method lookup
        if mangled_name in self.global_scope:
            method_func = self.global_scope[mangled_name]
            return obj_ptr, method_func

        # 2. Try field access via GEP + load
        if obj_type_name in self.class_layouts:
            layout = self.class_layouts[obj_type_name]
            if attr_name in layout:
                field_index, field_type = layout[attr_name]
                field_ptr = self.builder.gep(obj_ptr,
                    [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), field_index)],
                    inbounds=True, name=f"{attr_name}.ptr")
                return self.builder.load(field_ptr, name=attr_name)

        raise NameError(f"Attribute '{attr_name}' not found on type '{obj_type_name}'")

    def visit_Subscript(self, node):
        ptr = self.visit(node.value)
        idx = self.visit(node.slice)

        addr = self.builder.gep(ptr, [idx], inbounds=True, name='addr')
        return self.builder.load(addr, name='val')

    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_Call(self, node):
        callee = self.visit(node.func)

        if isinstance(callee, tuple):
            obj_ptr, method_func = callee
            args = [obj_ptr] + [self.visit(arg) for arg in node.args]
            return self.builder.call(method_func, args, 'calltmp')

        elif isinstance(callee, ir.PointerType) and isinstance(callee.pointee, ir.IdentifiedStructType):
            obj_type = callee.pointee

            null_ptr = ir.Constant(callee, None)
            size_ptr = self.builder.gep(null_ptr, [ir.Constant(ir.IntType(32), 1)], inbounds=False)
            size = self.builder.ptrtoint(size_ptr, self.types['int'], name="size")

            # Use arena allocator with current arena tag
            arena_alloc = self.global_scope['lambpie_arena_alloc']
            tag = ir.Constant(ir.IntType(32), self.current_arena)
            obj_ptr_void = self.builder.call(arena_alloc, [tag, size], 'arena_call')
            obj_ptr = self.builder.bitcast(obj_ptr_void, callee, 'obj_ptr')

            init_name = f"{obj_type.name}___init__"
            if init_name in self.global_scope:
                init_func = self.global_scope[init_name]
                raw_args = [self.visit(arg) for arg in node.args]
                # Coerce args to match __init__ parameter types
                # __init__ params: [self_ptr, field1_type, field2_type, ...]
                init_param_types = init_func.type.pointee.args
                coerced_args = []
                for i, arg in enumerate(raw_args):
                    expected = init_param_types[i + 1]  # +1 to skip self
                    coerced_args.append(self._coerce_arg(arg, expected))
                init_args = [obj_ptr] + coerced_args
                self.builder.call(init_func, init_args)

            return obj_ptr

        else:
            callee_func = callee
            args = [self.visit(arg) for arg in node.args]
            return self.builder.call(callee_func, args, 'calltmp')

    def visit_Pass(self, node):
        pass

    def visit_Expr(self, node):
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        var_name = node.target.id
        var_type = self._get_type(node.annotation)

        var_alloc = self.builder.alloca(var_type, name=var_name)
        self.local_scope[var_name] = var_alloc

        if node.value:
            value = self.visit(node.value)
            self.builder.store(value, var_alloc)

        # Tag variable with current arena context
        self.arena_tags[var_name] = self.current_arena

    def visit_Assign(self, node):
        var_name = node.targets[0].id

        if var_name not in self.local_scope:
            raise NameError(f"Cannot assign to undeclared variable: {var_name}")

        var_alloc = self.local_scope[var_name]
        value = self.visit(node.value)
        self.builder.store(value, var_alloc)

    def compile(self, tree):
        self.visit(tree)
        self._synthesize_lambda_entry(tree)
        return self.module

    def get_metadata(self):
        """Return metadata dict describing the compiled handler."""
        if self._handle_node is None:
            return None
        handle_node = self._handle_node
        event_type_name = handle_node.args.args[0].annotation.id
        response_type_name = handle_node.returns.id

        def _fields_dict(layout):
            result = {}
            for name, (idx, typ) in layout.items():
                if name == '__ref_count__':
                    continue
                if self._is_str_type(typ):
                    result[name] = 'str'
                elif typ == self.types['int']:
                    result[name] = 'int'
                else:
                    result[name] = str(typ)
            return result

        return {
            "trigger": "direct",
            "event_type": event_type_name,
            "response_type": response_type_name,
            "event_fields": _fields_dict(self.class_layouts[event_type_name]),
            "response_fields": _fields_dict(self.class_layouts[response_type_name]),
        }

    def _synthesize_lambda_entry(self, tree):
        """Synthesize lambpie_init() and lambpie_handle() as extern "C" functions.

        Flat module model:
        - lambpie_init: compiles collected top-level init statements
        - lambpie_handle: deserializes event JSON → typed struct, calls handle(),
          serializes response struct → JSON, returns length
        """
        if self._handle_node is None:
            raise RuntimeError("No handle() function found. Every .pie file must define a top-level handle() function.")

        # Extract event and response type names from handle() annotations
        handle_node = self._handle_node
        if not handle_node.args.args or not handle_node.args.args[0].annotation:
            raise RuntimeError("handle() must have a typed event parameter, e.g. def handle(event: Request) -> Response")
        if not handle_node.returns:
            raise RuntimeError("handle() must have a return type annotation, e.g. def handle(event: Request) -> Response")

        event_type_name = handle_node.args.args[0].annotation.id
        response_type_name = handle_node.returns.id

        if event_type_name not in self.class_layouts:
            raise RuntimeError(f"Event type '{event_type_name}' not defined. Define it as a class with typed fields.")
        if response_type_name not in self.class_layouts:
            raise RuntimeError(f"Response type '{response_type_name}' not defined. Define it as a class with typed fields.")

        event_layout = self.class_layouts[event_type_name]
        response_layout = self.class_layouts[response_type_name]
        event_ptr_type = self.types[event_type_name]
        response_ptr_type = self.types[response_type_name]

        i8_ptr = self.types['__ptr__']
        i64 = self.types['int']
        i32 = ir.IntType(32)

        # --- Compile handle() function first (so it's available for calling) ---
        self.visit_FunctionDef(handle_node)

        # --- Declare JSON C functions ---
        # json_get_str(json, json_len, key, key_len, *out_len) -> char*
        size_t_ptr = ir.PointerType(i64)
        json_get_str_type = ir.FunctionType(i8_ptr, [i8_ptr, i64, i8_ptr, i64, size_t_ptr])
        json_get_str = ir.Function(self.module, json_get_str_type, name='json_get_str')

        # json_get_int(json, json_len, key, key_len) -> int64_t
        json_get_int_type = ir.FunctionType(i64, [i8_ptr, i64, i8_ptr, i64])
        json_get_int = ir.Function(self.module, json_get_int_type, name='json_get_int')

        # json_open(buf, pos) -> pos
        json_open_type = ir.FunctionType(i64, [i8_ptr, i64])
        json_open = ir.Function(self.module, json_open_type, name='json_open')

        # json_write_str(buf, pos, key, key_len, val, val_len) -> pos
        json_write_str_type = ir.FunctionType(i64, [i8_ptr, i64, i8_ptr, i64, i8_ptr, i64])
        json_write_str = ir.Function(self.module, json_write_str_type, name='json_write_str')

        # json_write_int(buf, pos, key, key_len, val) -> pos
        json_write_int_type = ir.FunctionType(i64, [i8_ptr, i64, i8_ptr, i64, i64])
        json_write_int = ir.Function(self.module, json_write_int_type, name='json_write_int')

        # json_close(buf, pos) -> pos
        json_close_type = ir.FunctionType(i64, [i8_ptr, i64])
        json_close = ir.Function(self.module, json_close_type, name='json_close')

        # --- lambpie_init() ---
        init_func_type = ir.FunctionType(ir.VoidType(), [])
        init_func = ir.Function(self.module, init_func_type, name='lambpie_init')
        entry = init_func.append_basic_block(name='entry')
        self.builder = ir.IRBuilder(entry)
        self.local_scope = {}
        self.current_arena = self.ARENA_STATIC

        # Compile collected init statements
        for stmt in self._init_stmts:
            self.visit(stmt)

        if not self.builder.block.is_terminated:
            self.builder.ret_void()

        # --- lambpie_handle(event_ptr, event_len, response_ptr, response_cap) -> i64 ---
        handle_func_type = ir.FunctionType(i64, [i8_ptr, i64, i8_ptr, i64])
        handle_func = ir.Function(self.module, handle_func_type, name='lambpie_handle')
        handle_func.args[0].name = 'event_ptr'
        handle_func.args[1].name = 'event_len'
        handle_func.args[2].name = 'response_ptr'
        handle_func.args[3].name = 'response_cap'

        entry = handle_func.append_basic_block(name='entry')
        self.builder = ir.IRBuilder(entry)
        self.local_scope = {}
        self.current_arena = self.ARENA_REQ

        event_ptr_val = handle_func.args[0]
        event_len_val = handle_func.args[1]
        response_ptr_val = handle_func.args[2]

        # --- Deserialize event JSON → typed struct ---
        # Allocate event struct on REQ arena
        null_ptr = ir.Constant(event_ptr_type, None)
        size_ptr = self.builder.gep(null_ptr, [ir.Constant(i32, 1)], inbounds=False)
        size = self.builder.ptrtoint(size_ptr, i64, name='event.size')
        arena_alloc = self.global_scope['lambpie_arena_alloc']
        tag = ir.Constant(i32, self.ARENA_REQ)
        event_raw = self.builder.call(arena_alloc, [tag, size], 'event.raw')
        event_struct = self.builder.bitcast(event_raw, event_ptr_type, 'event.struct')

        # Set ref_count = 1
        ref_ptr = self.builder.gep(event_struct,
            [ir.Constant(i32, 0), ir.Constant(i32, 0)], inbounds=True, name='event.rc.ptr')
        self.builder.store(ir.Constant(i32, 1), ref_ptr)

        # For each field in event type, deserialize from JSON
        event_fields = [(name, idx, typ) for name, (idx, typ) in event_layout.items()
                        if name != '__ref_count__']
        event_fields.sort(key=lambda f: f[1])

        for field_name, field_idx, field_type in event_fields:
            # Create global string constant for field key
            key_str = field_name + '\0'
            key_const = ir.Constant(ir.ArrayType(ir.IntType(8), len(key_str)),
                                    bytearray(key_str, 'utf8'))
            self._name_counter += 1
            key_global = ir.GlobalVariable(self.module, key_const.type, name=f".key.{self._name_counter}")
            key_global.linkage = 'private'
            key_global.initializer = key_const
            key_ptr = key_global.gep([ir.Constant(i32, 0), ir.Constant(i32, 0)])
            key_len = ir.Constant(i64, len(field_name))

            field_ptr = self.builder.gep(event_struct,
                [ir.Constant(i32, 0), ir.Constant(i32, field_idx)],
                inbounds=True, name=f'event.{field_name}.ptr')

            if self._is_str_type(field_type):
                # str field: json_get_str → allocate str struct
                out_len_alloc = self.builder.alloca(i64, name=f'{field_name}.len.out')
                str_data = self.builder.call(json_get_str,
                    [event_ptr_val, event_len_val, key_ptr, key_len, out_len_alloc],
                    f'{field_name}.data')
                str_len = self.builder.load(out_len_alloc, name=f'{field_name}.len')

                # Allocate str struct on REQ arena
                str_ptr_type = self.types['str']
                str_null = ir.Constant(str_ptr_type, None)
                str_size_ptr = self.builder.gep(str_null, [ir.Constant(i32, 1)], inbounds=False)
                str_size = self.builder.ptrtoint(str_size_ptr, i64, name=f'{field_name}.str.size')
                str_raw = self.builder.call(arena_alloc, [tag, str_size], f'{field_name}.str.raw')
                str_struct = self.builder.bitcast(str_raw, str_ptr_type, f'{field_name}.str')

                str_layout = self.class_layouts['str']
                # Set ref_count = 1
                src_rc_ptr = self.builder.gep(str_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, str_layout['__ref_count__'][0])],
                    inbounds=True)
                self.builder.store(ir.Constant(i32, 1), src_rc_ptr)
                # Set len
                src_len_ptr = self.builder.gep(str_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, str_layout['len'][0])],
                    inbounds=True)
                self.builder.store(str_len, src_len_ptr)
                # Set buffer
                src_buf_ptr = self.builder.gep(str_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, str_layout['buffer'][0])],
                    inbounds=True)
                self.builder.store(str_data, src_buf_ptr)

                self.builder.store(str_struct, field_ptr)

            elif field_type == i64:
                # int field: json_get_int
                int_val = self.builder.call(json_get_int,
                    [event_ptr_val, event_len_val, key_ptr, key_len],
                    f'{field_name}.val')
                self.builder.store(int_val, field_ptr)
            else:
                raise RuntimeError(f"Unsupported event field type for JSON deserialization: {field_name}")

        # --- Call user's handle(event) → response struct ---
        user_handle = self.global_scope['handle']
        response_struct = self.builder.call(user_handle, [event_struct], 'response')

        # --- Serialize response struct → JSON ---
        pos = self.builder.call(json_open, [response_ptr_val, ir.Constant(i64, 0)], 'pos')

        response_fields = [(name, idx, typ) for name, (idx, typ) in response_layout.items()
                           if name != '__ref_count__']
        response_fields.sort(key=lambda f: f[1])

        for field_name, field_idx, field_type in response_fields:
            # Create global string constant for field key
            key_str = field_name + '\0'
            key_const = ir.Constant(ir.ArrayType(ir.IntType(8), len(key_str)),
                                    bytearray(key_str, 'utf8'))
            self._name_counter += 1
            key_global = ir.GlobalVariable(self.module, key_const.type, name=f".key.{self._name_counter}")
            key_global.linkage = 'private'
            key_global.initializer = key_const
            key_ptr = key_global.gep([ir.Constant(i32, 0), ir.Constant(i32, 0)])
            key_len = ir.Constant(i64, len(field_name))

            if self._is_str_type(field_type):
                # Load str struct pointer, then extract buffer and len
                str_field_ptr = self.builder.gep(response_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, field_idx)],
                    inbounds=True, name=f'resp.{field_name}.ptr')
                str_struct = self.builder.load(str_field_ptr, name=f'resp.{field_name}')

                str_layout = self.class_layouts['str']
                buf_ptr = self.builder.gep(str_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, str_layout['buffer'][0])],
                    inbounds=True, name=f'resp.{field_name}.buf.ptr')
                buf_val = self.builder.load(buf_ptr, name=f'resp.{field_name}.buf')
                len_ptr = self.builder.gep(str_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, str_layout['len'][0])],
                    inbounds=True, name=f'resp.{field_name}.len.ptr')
                len_val = self.builder.load(len_ptr, name=f'resp.{field_name}.len')

                pos = self.builder.call(json_write_str,
                    [response_ptr_val, pos, key_ptr, key_len, buf_val, len_val],
                    'pos')

            elif field_type == i64:
                int_field_ptr = self.builder.gep(response_struct,
                    [ir.Constant(i32, 0), ir.Constant(i32, field_idx)],
                    inbounds=True, name=f'resp.{field_name}.ptr')
                int_val = self.builder.load(int_field_ptr, name=f'resp.{field_name}')

                pos = self.builder.call(json_write_int,
                    [response_ptr_val, pos, key_ptr, key_len, int_val],
                    'pos')
            else:
                raise RuntimeError(f"Unsupported response field type for JSON serialization: {field_name}")

        pos = self.builder.call(json_close, [response_ptr_val, pos], 'pos.final')
        self.builder.ret(pos)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Lambpie Compiler")
    parser.add_argument('source_file', help="The source .pie file to compile.")
    parser.add_argument('-o', '--output', dest='output_name', default='handler',
                        help="The base name for the output files (default: handler).")
    parser.add_argument('--target', dest='target_triple', default=None,
                        help="Target triple (default: x86_64-unknown-linux-gnu).")

    args = parser.parse_args()

    source_file = args.source_file
    output_name = args.output_name

    if not os.path.exists(source_file):
        print(f"Error: File not found - {source_file}")
        sys.exit(1)

    # 1. Read builtins and source file
    builtin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'builtins.pie')
    with open(builtin_path, 'r') as f:
        builtin_code = f.read()
    with open(source_file, 'r') as f:
        source_code = f.read()

    # 2. Parse both into ASTs
    builtin_ast = ast.parse(builtin_code, filename=builtin_path)
    source_ast = ast.parse(source_code, filename=source_file)

    # 3. Combine ASTs
    combined_ast = ast.Module(
        body=builtin_ast.body + source_ast.body,
        type_ignores=[]
    )

    # 4. Compile
    compiler = Compiler(target_triple=args.target_triple)
    llvm_module = compiler.compile(combined_ast)

    print("\n--- LLVM IR ---")
    print(str(llvm_module))

    # 5. Save LLVM IR to file
    ir_filename = f"{output_name}.ll"
    os.makedirs(os.path.dirname(os.path.abspath(ir_filename)), exist_ok=True)
    with open(ir_filename, "w") as f:
        f.write(str(llvm_module))
    print(f"\nLLVM IR saved to {ir_filename}")

    # 6. Save metadata
    metadata = compiler.get_metadata()
    if metadata:
        meta_filename = f"{output_name}.lambpie.json"
        with open(meta_filename, "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Metadata saved to {meta_filename}")

    print("\nTo build the Lambda bootstrap binary:")
    print(f"  python scripts/build.py {source_file}")


if __name__ == '__main__':
    main()
