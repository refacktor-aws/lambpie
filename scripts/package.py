"""Package the bootstrap binary into a Lambda deployment zip.

Usage: python scripts/package.py [--bootstrap target/bootstrap] [--output target/function.zip]
"""

import argparse
import io
import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description='Package bootstrap into Lambda zip')
    parser.add_argument('--bootstrap', default=os.path.join(REPO_ROOT, 'target', 'bootstrap'),
                        help='Path to bootstrap binary')
    parser.add_argument('--output', default=os.path.join(REPO_ROOT, 'target', 'function.zip'),
                        help='Output zip path')
    args = parser.parse_args()

    if not os.path.exists(args.bootstrap):
        print(f"Error: bootstrap binary not found at {args.bootstrap}")
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('bootstrap')
        info.external_attr = 0o755 << 16  # executable permission
        with open(args.bootstrap, 'rb') as f:
            zf.writestr(info, f.read())

    size = os.path.getsize(args.output)
    print(f"Packaged {args.output} ({size:,} bytes)")


if __name__ == '__main__':
    main()
