"""Build and test a .pie handler locally using the mock Lambda runtime.

Usage: python scripts/test.py tests/echo.pie
"""

import argparse
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description='Build and test a .pie handler locally')
    parser.add_argument('source_file', help='The .pie source file to test')
    args = parser.parse_args()

    # Step 1: Build
    build_script = os.path.join(REPO_ROOT, 'scripts', 'build.py')
    print("=== Building ===")
    result = subprocess.run([sys.executable, build_script, args.source_file])
    if result.returncode != 0:
        print("Build failed.")
        sys.exit(result.returncode)

    # Step 2: Test with mock runtime
    bootstrap = os.path.join(REPO_ROOT, 'target', 'bootstrap')
    test_runtime = os.path.join(REPO_ROOT, 'tests', 'test_runtime.py')

    if not os.path.exists(bootstrap):
        print(f"Error: bootstrap binary not found at {bootstrap}")
        sys.exit(1)

    print("\n=== Testing with mock Lambda runtime ===")
    result = subprocess.run([sys.executable, test_runtime, bootstrap])
    if result.returncode != 0:
        print("Test failed.")
        sys.exit(result.returncode)

    print("\nAll tests passed.")


if __name__ == '__main__':
    main()
