"""Deploy and verify the echo handler on localstack Lambda.

Usage: python scripts/verify.py [--endpoint http://localhost:4577]

Deploys target/function.zip to localstack, invokes with a test event,
and asserts the response is an exact echo of the input.
"""

import argparse
import json
import os
import sys
import time

import boto3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FUNCTION_NAME = 'lambpie-echo-test'
TEST_EVENT = {"message": "hello from lambpie", "number": 42}
EXPECTED_RESPONSE = {"status": "ok", "echo": "hello from lambpie", "doubled": 84}

# Explicit Lambda configuration — no magic numbers buried in call sites.
FUNCTION_MEMORY_MB = 128
FUNCTION_TIMEOUT_SECONDS = 30
FUNCTION_ARCHITECTURE = 'x86_64'
FUNCTION_RUNTIME = 'provided.al2023'

# Localstack uses a fake account ID for ARNs.
LOCALSTACK_ROLE_ARN = 'arn:aws:iam::000000000000:role/lambda-role'

FUNCTION_ACTIVE_POLL_INTERVAL_SECONDS = 1
FUNCTION_ACTIVE_MAX_WAIT_SECONDS = 60
LOCALSTACK_READY_POLL_INTERVAL_SECONDS = 1
LOCALSTACK_READY_MAX_RETRIES = 30


def wait_for_localstack(endpoint: str) -> None:
    """Block until localstack Lambda service is ready, or raise on timeout."""
    import urllib.request
    import urllib.error

    health_url = f"{endpoint}/_localstack/health"
    for attempt in range(1, LOCALSTACK_READY_MAX_RETRIES + 1):
        try:
            resp = urllib.request.urlopen(health_url, timeout=2)
            data = json.loads(resp.read())
            if data.get("services", {}).get("lambda") in ("available", "running", "ready"):
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(LOCALSTACK_READY_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"localstack not ready after {LOCALSTACK_READY_MAX_RETRIES}s at {endpoint}"
    )


def wait_for_function_active(client, function_name: str) -> None:
    """Poll until the function reaches Active state or exit on failure/timeout."""
    deadline = time.monotonic() + FUNCTION_ACTIVE_MAX_WAIT_SECONDS
    state = None
    while True:
        info = client.get_function(FunctionName=function_name)
        state = info['Configuration']['State']
        if state == 'Active':
            return
        if state == 'Failed':
            reason = info['Configuration'].get('StateReason', 'unknown')
            sys.exit(f"Error: function entered Failed state: {reason}")
        if time.monotonic() > deadline:
            sys.exit(
                f"Error: function still in state '{state}' after "
                f"{FUNCTION_ACTIVE_MAX_WAIT_SECONDS}s timeout."
            )
        time.sleep(FUNCTION_ACTIVE_POLL_INTERVAL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description='Verify lambpie echo handler on localstack')
    parser.add_argument('--endpoint', default='http://localhost:4577',
                        help='Localstack endpoint URL')
    parser.add_argument('--zip', default=os.path.join(REPO_ROOT, 'target', 'function.zip'),
                        help='Path to function.zip (default: target/function.zip)')
    args = parser.parse_args()

    if not os.path.exists(args.zip):
        sys.exit(f"Error: {args.zip} not found. Run 'make package' first.")

    print(f"Waiting for localstack at {args.endpoint}...")
    wait_for_localstack(args.endpoint)
    print("localstack is ready.")

    session = boto3.Session(
        aws_access_key_id='test',
        aws_secret_access_key='test',
        region_name='us-east-1',
    )
    client = session.client('lambda', endpoint_url=args.endpoint)

    with open(args.zip, 'rb') as f:
        zip_bytes = f.read()
    print(f"Deploying {FUNCTION_NAME} ({len(zip_bytes):,} bytes)...")

    # Delete any leftover function from a previous run.
    try:
        client.delete_function(FunctionName=FUNCTION_NAME)
        print(f"Deleted existing function {FUNCTION_NAME}.")
    except client.exceptions.ResourceNotFoundException:
        pass

    client.create_function(
        FunctionName=FUNCTION_NAME,
        Runtime=FUNCTION_RUNTIME,
        Role=LOCALSTACK_ROLE_ARN,
        Handler='bootstrap',
        Code={'ZipFile': zip_bytes},
        MemorySize=FUNCTION_MEMORY_MB,
        Timeout=FUNCTION_TIMEOUT_SECONDS,
        Architectures=[FUNCTION_ARCHITECTURE],
    )
    print(f"Function {FUNCTION_NAME} created.")

    print("Waiting for function to become Active...")
    wait_for_function_active(client, FUNCTION_NAME)
    print("Function is Active.")

    payload_bytes = json.dumps(TEST_EVENT).encode()
    print(f"Invoking with: {payload_bytes.decode()}")

    response = client.invoke(
        FunctionName=FUNCTION_NAME,
        Payload=payload_bytes,
    )

    response_payload = response['Payload'].read()
    status_code = response.get('StatusCode', 0)
    function_error = response.get('FunctionError', '')

    print(f"StatusCode: {status_code}")

    if function_error:
        sys.exit(
            f"Error: Lambda returned FunctionError={function_error!r}\n"
            f"Response body: {response_payload.decode()}"
        )

    response_text = response_payload.decode()
    print(f"Response: {response_text}")

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError:
        sys.exit(f"Error: response is not valid JSON: {response_text!r}")

    if response_data != EXPECTED_RESPONSE:
        sys.exit(f"FAIL: expected {EXPECTED_RESPONSE}, got {response_data}")

    print("PASS")

    # Cleanup — best-effort; don't mask the PASS result with a cleanup error.
    try:
        client.delete_function(FunctionName=FUNCTION_NAME)
        print("Cleanup complete.")
    except Exception as exc:
        print(f"Warning: cleanup failed (non-fatal): {exc}")


if __name__ == '__main__':
    main()
