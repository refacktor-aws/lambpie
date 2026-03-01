import ast
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

    def _get_type(self, annotation_node):
        if isinstance(annotation_node, ast.Name):
            type_name = annotation_node.id
            if type_name == 'bytes':
                return self.types['__ptr__']
            if type_name in self.types:
                return self.types[type_name]
            else:
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
            if not is_main_block:
                self.visit(sub_node)

    def visit_ImportFrom(self, node):
        if node.module == 'C':
            for alias in node.names:
                name = alias.name
                if name == 'printf':
                    func_type = ir.FunctionType(ir.IntType(32), [self.types['__ptr__']], var_arg=True)
                elif name == 'atoi':
                    func_type = ir.FunctionType(self.types['int'], [self.types['__ptr__']])
                else:
                    func_type = ir.FunctionType(ir.VoidType(), [])

                if name not in self.global_scope:
                    self.global_scope[name] = ir.Function(self.module, func_type, name=name)
        else:
            pass

    def visit_ClassDef(self, node):
        print(f"Found class: {node.name}")

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
        print(f"  - Layout for {class_name}: {attributes}")

        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef):
                self.visit_FunctionDef(stmt, class_name=class_name)

    def visit_FunctionDef(self, node, class_name=None):
        func_name = node.name
        if class_name:
            func_name = f"{class_name}_{func_name}"

        print(f"Found function/method: {func_name}")

        # Set arena context based on which Handler method we're compiling
        saved_arena = self.current_arena
        if class_name == 'Handler':
            if node.name == 'handle':
                self.current_arena = self.ARENA_REQ
            else:
                # __init__, init, and any other Handler methods use static arena
                self.current_arena = self.ARENA_STATIC

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

            global_var = ir.GlobalVariable(self.module, c_string.type, name=f".str.{abs(hash(node.value))}")
            global_var.linkage = 'private'
            global_var.initializer = c_string

            return global_var.gep([ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), 0)])
        elif isinstance(node.value, bytes):
            # Handle bytes literals like b'{"status": "ok"}'
            byte_vals = list(node.value)
            c_bytes = ir.Constant(ir.ArrayType(ir.IntType(8), len(byte_vals)),
                                  byte_vals)

            global_var = ir.GlobalVariable(self.module, c_bytes.type,
                                           name=f".bytes.{abs(hash(node.value))}")
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

        method_name = node.attr
        mangled_name = f"{obj_type_name}_{method_name}"

        if mangled_name in self.global_scope:
            method_func = self.global_scope[mangled_name]
            return obj_ptr, method_func
        else:
            raise NameError(f"Method '{method_name}' not found on object of type '{obj_type_name}'")

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
                init_args = [obj_ptr] + [self.visit(arg) for arg in node.args]
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

    def _synthesize_lambda_entry(self, tree):
        """Synthesize lambpie_init() and lambpie_handle() as extern "C" functions.

        lambpie_init: Instantiates Handler, calls Handler.init(), stores in global.
        lambpie_handle(event_ptr, event_len, response_ptr, response_cap) -> response_len:
            Calls Handler.handle(event_ptr, event_len) which returns (ptr, len),
            copies result to response buffer, returns length.

        For M1 (echo handler), handle() receives raw event bytes as __ptr__ + len
        and returns raw response bytes as __ptr__ (pointing to response buffer).
        """
        # Verify Handler class exists
        if 'Handler' not in self.types:
            print("Warning: No Handler class found. Skipping Lambda entry synthesis.")
            return

        handler_ptr_type = self.types['Handler']
        handler_struct_type = handler_ptr_type.pointee

        # --- Create global to hold the Handler instance ---
        handler_global = ir.GlobalVariable(self.module, handler_ptr_type, name='lambpie_handler')
        handler_global.linkage = 'internal'
        handler_global.initializer = ir.Constant(handler_ptr_type, None)

        # --- lambpie_init() ---
        init_func_type = ir.FunctionType(ir.VoidType(), [])
        init_func = ir.Function(self.module, init_func_type, name='lambpie_init')
        entry = init_func.append_basic_block(name='entry')
        self.builder = ir.IRBuilder(entry)
        self.local_scope = {}

        # Allocate Handler instance
        null_ptr = ir.Constant(handler_ptr_type, None)
        size_ptr = self.builder.gep(null_ptr, [ir.Constant(ir.IntType(32), 1)], inbounds=False)
        size = self.builder.ptrtoint(size_ptr, self.types['int'], name='size')

        # Allocate Handler on static arena (cold-start)
        arena_alloc = self.global_scope['lambpie_arena_alloc']
        tag = ir.Constant(ir.IntType(32), self.ARENA_STATIC)
        obj_ptr_void = self.builder.call(arena_alloc, [tag, size], 'arena_call')
        handler_instance = self.builder.bitcast(obj_ptr_void, handler_ptr_type, 'handler')

        # Call Handler.__init__ if it exists
        init_method = 'Handler___init__'
        if init_method in self.global_scope:
            self.builder.call(self.global_scope[init_method], [handler_instance])

        # Call Handler.init if it exists
        init_user_method = 'Handler_init'
        if init_user_method in self.global_scope:
            self.builder.call(self.global_scope[init_user_method], [handler_instance])

        # Store handler instance in global
        self.builder.store(handler_instance, handler_global)
        self.builder.ret_void()

        # --- lambpie_handle(event_ptr, event_len, response_ptr, response_cap) -> usize ---
        i8_ptr = self.types['__ptr__']
        i64 = self.types['int']
        handle_func_type = ir.FunctionType(i64, [i8_ptr, i64, i8_ptr, i64])
        handle_func = ir.Function(self.module, handle_func_type, name='lambpie_handle')

        # Name the arguments
        handle_func.args[0].name = 'event_ptr'
        handle_func.args[1].name = 'event_len'
        handle_func.args[2].name = 'response_ptr'
        handle_func.args[3].name = 'response_cap'

        entry = handle_func.append_basic_block(name='entry')
        self.builder = ir.IRBuilder(entry)
        self.local_scope = {}

        # Alloca args for easy access
        event_ptr_alloc = self.builder.alloca(i8_ptr, name='event_ptr.addr')
        self.builder.store(handle_func.args[0], event_ptr_alloc)
        event_len_alloc = self.builder.alloca(i64, name='event_len.addr')
        self.builder.store(handle_func.args[1], event_len_alloc)
        response_ptr_alloc = self.builder.alloca(i8_ptr, name='response_ptr.addr')
        self.builder.store(handle_func.args[2], response_ptr_alloc)
        response_cap_alloc = self.builder.alloca(i64, name='response_cap.addr')
        self.builder.store(handle_func.args[3], response_cap_alloc)

        # Load handler from global
        handler_instance = self.builder.load(handler_global, name='handler')

        # Load event args
        event_ptr_val = self.builder.load(event_ptr_alloc, name='event_ptr')
        event_len_val = self.builder.load(event_len_alloc, name='event_len')

        # Call Handler.handle(self, event_ptr, event_len) -> i64 (response length)
        # The handler receives the event pointer and length, writes to response buffer,
        # and returns the response length.
        handle_method = 'Handler_handle'
        if handle_method not in self.global_scope:
            raise RuntimeError("Handler class must define a handle() method")

        response_ptr_val = self.builder.load(response_ptr_alloc, name='response_ptr')
        response_cap_val = self.builder.load(response_cap_alloc, name='response_cap')

        result_len = self.builder.call(
            self.global_scope[handle_method],
            [handler_instance, event_ptr_val, event_len_val, response_ptr_val, response_cap_val],
            'result_len'
        )

        self.builder.ret(result_len)

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

    print("\nTo build the Lambda bootstrap binary:")
    print(f"  python scripts/build.py {source_file}")


if __name__ == '__main__':
    main()
