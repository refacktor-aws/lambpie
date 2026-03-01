"""Build a .pie source file into a Lambda bootstrap binary.

Usage: python scripts/build.py tests/echo.pie [-o output_dir]

Steps:
  1. Compile .pie -> .ll (LLVM IR) via compiler.py
  2. Compile .ll -> .o (object file) via llc
  3. Link .o into bootstrap binary via cargo build (shim crate)
"""

import argparse
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPILER = os.path.join(REPO_ROOT, 'compiler.py')
SHIM_DIR = os.path.join(REPO_ROOT, 'runtime', 'shim')

TARGET_TRIPLE = 'x86_64-unknown-linux-gnu'


def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}")
        sys.exit(result.returncode)
    return result


def main():
    parser = argparse.ArgumentParser(description='Build a .pie file into a Lambda bootstrap binary')
    parser.add_argument('source_file', help='The .pie source file to compile')
    parser.add_argument('-o', '--output-dir', default=os.path.join(REPO_ROOT, 'target'),
                        help='Output directory for the bootstrap binary')
    parser.add_argument('--target', default=TARGET_TRIPLE,
                        help=f'Target triple (default: {TARGET_TRIPLE})')
    parser.add_argument('--ir-only', action='store_true',
                        help='Stop after generating LLVM IR (skip linking)')
    args = parser.parse_args()

    source = os.path.abspath(args.source_file)
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    basename = os.path.splitext(os.path.basename(source))[0]
    ll_file = os.path.join(output_dir, f'{basename}.ll')
    obj_file = os.path.join(output_dir, f'{basename}.o')

    # Step 1: .pie -> .ll
    print("\n[1/3] Compiling .pie to LLVM IR...")
    run([sys.executable, COMPILER, source, '-o', os.path.join(output_dir, basename)])

    if args.ir_only:
        print(f"\nLLVM IR written to {ll_file}")
        return

    # Step 2: .ll -> .o
    print("\n[2/3] Assembling LLVM IR to object file...")
    run(['llc', ll_file, '-filetype=obj', '-o', obj_file,
         f'-mtriple={args.target}', '-relocation-model=pic'])

    # Step 3: cargo build with handler.o linked in
    print("\n[3/3] Linking bootstrap binary via cargo...")
    env = os.environ.copy()
    env['LAMBPIE_HANDLER_OBJ'] = obj_file

    cargo_args = [
        'cargo', 'build', '--release',
        '--target', args.target,
        '--manifest-path', os.path.join(SHIM_DIR, 'Cargo.toml'),
        '--target-dir', os.path.join(output_dir, 'cargo-target'),
    ]
    run(cargo_args, env=env)

    # Copy the bootstrap binary
    bootstrap_src = os.path.join(output_dir, 'cargo-target', args.target, 'release', 'bootstrap')
    bootstrap_dst = os.path.join(output_dir, 'bootstrap')

    if os.path.exists(bootstrap_src):
        import shutil
        shutil.copy2(bootstrap_src, bootstrap_dst)
        print(f"\nBootstrap binary: {bootstrap_dst}")

        # Try to strip
        try:
            run(['strip', '-s', bootstrap_dst])
            size = os.path.getsize(bootstrap_dst)
            print(f"Stripped size: {size:,} bytes ({size / 1024:.1f} KB)")
        except FileNotFoundError:
            size = os.path.getsize(bootstrap_dst)
            print(f"Size: {size:,} bytes ({size / 1024:.1f} KB)")
    else:
        print(f"\nWarning: bootstrap binary not found at {bootstrap_src}")
        print("Check cargo build output above for errors.")


if __name__ == '__main__':
    main()
