"""Deploy a built bootstrap binary to AWS Lambda.

Usage: python scripts/deploy.py --function-name my-function [--build tests/echo.pie]
"""

import argparse
import io
import os
import sys
import zipfile

import boto3


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_zip(bootstrap_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('bootstrap')
        info.external_attr = 0o755 << 16  # executable permission
        with open(bootstrap_path, 'rb') as f:
            zf.writestr(info, f.read())
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description='Deploy lambpie bootstrap to AWS Lambda')
    parser.add_argument('--function-name', required=True, help='Lambda function name')
    parser.add_argument('--build', help='Build this .pie file first (runs build.py)')
    parser.add_argument('--bootstrap', default=os.path.join(REPO_ROOT, 'target', 'bootstrap'),
                        help='Path to the bootstrap binary')
    parser.add_argument('--region', default=None, help='AWS region')
    parser.add_argument('--role', default=None, help='IAM role ARN for creating new functions')
    args = parser.parse_args()

    # Optionally build first
    if args.build:
        import subprocess
        build_script = os.path.join(REPO_ROOT, 'scripts', 'build.py')
        result = subprocess.run([sys.executable, build_script, args.build])
        if result.returncode != 0:
            sys.exit(result.returncode)

    if not os.path.exists(args.bootstrap):
        print(f"Error: bootstrap binary not found at {args.bootstrap}")
        print("Run build.py first or specify --bootstrap path")
        sys.exit(1)

    zip_bytes = create_zip(args.bootstrap)
    print(f"Deployment package: {len(zip_bytes):,} bytes ({len(zip_bytes) / 1024:.1f} KB)")

    session = boto3.Session(region_name=args.region)
    client = session.client('lambda')

    try:
        client.get_function(FunctionName=args.function_name)
        # Function exists, update it
        print(f"Updating function {args.function_name}...")
        client.update_function_code(
            FunctionName=args.function_name,
            ZipFile=zip_bytes,
        )
        print("Function updated.")
    except client.exceptions.ResourceNotFoundException:
        if not args.role:
            print("Error: Function does not exist and --role not specified.")
            print("Provide --role to create a new function.")
            sys.exit(1)

        print(f"Creating function {args.function_name}...")
        client.create_function(
            FunctionName=args.function_name,
            Runtime='provided.al2023',
            Role=args.role,
            Handler='bootstrap',
            Code={'ZipFile': zip_bytes},
            MemorySize=128,
            Timeout=10,
        )
        print("Function created.")

    # Test invocation
    print(f"\nInvoking {args.function_name}...")
    response = client.invoke(
        FunctionName=args.function_name,
        Payload=b'{"message": "hello from lambpie"}',
    )
    payload = response['Payload'].read().decode('utf-8')
    print(f"Response: {payload}")


if __name__ == '__main__':
    main()
