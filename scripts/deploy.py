"""Deploy a built bootstrap binary to AWS Lambda.

Usage:
    # Deploy to real AWS (create IAM role automatically):
    python scripts/deploy.py --function-name my-function --create-role

    # Deploy to real AWS (bring your own role):
    python scripts/deploy.py --function-name my-function --role arn:aws:iam::123:role/my-role

    # Deploy to localstack:
    python scripts/deploy.py --function-name my-function --endpoint-url http://localhost:4577

    # Build first, then deploy:
    python scripts/deploy.py --function-name my-function --build tests/echo.pie --create-role
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lambda runtime defaults — explicit, no magic numbers buried in code.
DEFAULT_MEMORY_MB = 128
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_ARCHITECTURE = 'x86_64'
DEFAULT_RUNTIME = 'provided.al2023'

# IAM managed policy that grants Lambda permission to write CloudWatch logs.
LAMBDA_BASIC_EXECUTION_POLICY_ARN = (
    'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
)

# Lambda trust policy: allows lambda.amazonaws.com to assume this role.
LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# IAM is eventually consistent. After create_role / attach_role_policy the
# role ARN may return "not authorized" errors for up to 10-15 seconds.
IAM_PROPAGATION_POLL_INTERVAL_SECONDS = 5
IAM_PROPAGATION_MAX_WAIT_SECONDS = 60


def create_zip(bootstrap_path: str) -> bytes:
    """Package the bootstrap binary into a Lambda deployment zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('bootstrap')
        info.external_attr = 0o755 << 16  # executable bit required by Lambda custom runtime
        with open(bootstrap_path, 'rb') as f:
            zf.writestr(info, f.read())
    return buf.getvalue()


def ensure_iam_role(iam_client, role_name: str) -> str:
    """Create or retrieve an IAM role suitable for Lambda execution.

    Returns the role ARN.  Attaches AWSLambdaBasicExecutionRole if the role
    was just created.  Does NOT re-attach on an existing role to stay
    idempotent and avoid spurious policy drift.
    """
    try:
        resp = iam_client.get_role(RoleName=role_name)
        role_arn = resp['Role']['Arn']
        print(f"IAM role already exists: {role_arn}")
        return role_arn
    except ClientError as e:
        if e.response['Error']['Code'] != 'NoSuchEntity':
            raise

    print(f"Creating IAM role: {role_name}")
    resp = iam_client.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
        Description='Execution role for lambpie Lambda functions',
    )
    role_arn = resp['Role']['Arn']
    print(f"  Role ARN: {role_arn}")

    print(f"Attaching managed policy: {LAMBDA_BASIC_EXECUTION_POLICY_ARN}")
    iam_client.attach_role_policy(
        RoleName=role_name,
        PolicyArn=LAMBDA_BASIC_EXECUTION_POLICY_ARN,
    )

    return role_arn


def wait_for_iam_propagation(lambda_client, role_arn: str, zip_bytes: bytes,
                              function_name_probe: str,
                              memory_mb: int, timeout_seconds: int,
                              architecture: str) -> None:
    """Poll Lambda create_function until IAM propagation succeeds.

    IAM role changes are eventually consistent across AWS services.
    Lambda rejects CreateFunction with InvalidParameterValueException
    ("The role defined for the function cannot be assumed by Lambda")
    until the role has propagated.  We retry until success or timeout.
    """
    deadline = time.monotonic() + IAM_PROPAGATION_MAX_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = lambda_client.create_function(
                FunctionName=function_name_probe,
                Runtime=DEFAULT_RUNTIME,
                Role=role_arn,
                Handler='bootstrap',
                Code={'ZipFile': zip_bytes},
                MemorySize=memory_mb,
                Timeout=timeout_seconds,
                Architectures=[architecture],
            )
            return resp
        except ClientError as e:
            code = e.response['Error']['Code']
            msg = e.response['Error']['Message']
            if code == 'InvalidParameterValueException' and 'cannot be assumed' in msg:
                elapsed = time.monotonic() - (deadline - IAM_PROPAGATION_MAX_WAIT_SECONDS)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    sys.exit(
                        f"Error: IAM role did not propagate within "
                        f"{IAM_PROPAGATION_MAX_WAIT_SECONDS}s. Last error: {msg}"
                    )
                print(
                    f"  IAM not yet propagated (attempt {attempt}, "
                    f"{elapsed:.0f}s elapsed) — retrying in "
                    f"{IAM_PROPAGATION_POLL_INTERVAL_SECONDS}s..."
                )
                time.sleep(IAM_PROPAGATION_POLL_INTERVAL_SECONDS)
            else:
                raise


def wait_for_function_active(lambda_client, function_name: str) -> None:
    """Poll until the function reaches Active state or raises on failure."""
    print(f"Waiting for {function_name} to become Active...")
    deadline = time.monotonic() + 120
    while True:
        info = lambda_client.get_function(FunctionName=function_name)
        state = info['Configuration']['State']
        if state == 'Active':
            print("Function is Active.")
            return
        if state == 'Failed':
            reason = info['Configuration'].get('StateReason', 'unknown')
            sys.exit(f"Error: function entered Failed state: {reason}")
        if time.monotonic() > deadline:
            sys.exit(f"Error: function still in state '{state}' after 120s timeout.")
        time.sleep(2)


def make_boto3_kwargs(region: str | None, endpoint_url: str | None) -> dict:
    """Build kwargs common to all boto3 client constructors."""
    kwargs = {}
    if region:
        kwargs['region_name'] = region
    if endpoint_url:
        kwargs['endpoint_url'] = endpoint_url
    return kwargs


def main():
    parser = argparse.ArgumentParser(
        description='Deploy a lambpie bootstrap binary to AWS Lambda',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--function-name', required=True,
                        help='Lambda function name')
    parser.add_argument('--build', metavar='PIE_FILE',
                        help='Build this .pie file first (runs scripts/build.py)')
    parser.add_argument('--bootstrap',
                        default=os.path.join(REPO_ROOT, 'target', 'bootstrap'),
                        help='Path to the bootstrap binary (default: target/bootstrap)')
    parser.add_argument('--region', default=None,
                        help='AWS region (default: from environment / AWS config)')
    parser.add_argument('--endpoint-url', default=None,
                        help='Override endpoint URL (e.g. http://localhost:4577 for localstack)')

    # Role: either supply an ARN directly, or let the script create/reuse one.
    role_group = parser.add_mutually_exclusive_group()
    role_group.add_argument('--role', metavar='ROLE_ARN',
                            help='IAM role ARN to use for the Lambda function')
    role_group.add_argument('--create-role', metavar='ROLE_NAME', nargs='?',
                            const='lambpie-lambda-execution-role',
                            help='Create (or reuse) an IAM role with this name '
                                 '(default name: lambpie-lambda-execution-role)')

    parser.add_argument('--memory', type=int, default=DEFAULT_MEMORY_MB,
                        help=f'Memory size in MB (default: {DEFAULT_MEMORY_MB})')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f'Timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})')
    parser.add_argument('--architecture', default=DEFAULT_ARCHITECTURE,
                        choices=['x86_64', 'arm64'],
                        help=f'CPU architecture (default: {DEFAULT_ARCHITECTURE})')
    args = parser.parse_args()

    # --- Build phase (optional) ---
    if args.build:
        import subprocess
        build_script = os.path.join(REPO_ROOT, 'scripts', 'build.py')
        result = subprocess.run([sys.executable, build_script, args.build])
        if result.returncode != 0:
            sys.exit(result.returncode)

    if not os.path.exists(args.bootstrap):
        sys.exit(
            f"Error: bootstrap binary not found at {args.bootstrap}\n"
            "Run 'make build' first or specify --bootstrap <path>."
        )

    zip_bytes = create_zip(args.bootstrap)
    print(f"Deployment package: {len(zip_bytes):,} bytes ({len(zip_bytes) / 1024:.1f} KB)")

    # --- Boto3 clients ---
    lambda_kwargs = make_boto3_kwargs(args.region, args.endpoint_url)
    lambda_client = boto3.client('lambda', **lambda_kwargs)

    # --- Resolve IAM role ---
    # For localstack we accept a fake ARN; for real AWS we need a real one.
    role_arn = None
    if args.role:
        role_arn = args.role
        print(f"Using supplied IAM role: {role_arn}")
    elif args.create_role is not None:
        # --create-role was given (with or without a custom name)
        iam_kwargs = make_boto3_kwargs(args.region, args.endpoint_url)
        iam_client = boto3.client('iam', **iam_kwargs)
        role_arn = ensure_iam_role(iam_client, args.create_role)
    # else: role_arn stays None — acceptable only if the function already exists

    # --- Deploy ---
    try:
        lambda_client.get_function(FunctionName=args.function_name)
        function_exists = True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
        function_exists = False

    if function_exists:
        print(f"Updating function code: {args.function_name}")
        lambda_client.update_function_code(
            FunctionName=args.function_name,
            ZipFile=zip_bytes,
            Architectures=[args.architecture],
        )
        print("Function code updated.")
        wait_for_function_active(lambda_client, args.function_name)
    else:
        if not role_arn:
            sys.exit(
                "Error: function does not exist and no IAM role was specified.\n"
                "Pass --role <ARN> or --create-role to create a new function."
            )
        print(f"Creating function: {args.function_name}")
        # Use the IAM-propagation-aware wrapper for real AWS; localstack does
        # not need retries but the wrapper is harmless there.
        wait_for_iam_propagation(
            lambda_client=lambda_client,
            role_arn=role_arn,
            zip_bytes=zip_bytes,
            function_name_probe=args.function_name,
            memory_mb=args.memory,
            timeout_seconds=args.timeout,
            architecture=args.architecture,
        )
        print(f"Function created: {args.function_name}")
        wait_for_function_active(lambda_client, args.function_name)

    print(f"\nDeploy complete: {args.function_name}")


if __name__ == '__main__':
    main()
