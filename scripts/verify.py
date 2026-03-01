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


def wait_for_localstack(endpoint, max_retries=30):
    """Wait for localstack to be ready."""
    import urllib.request
    import urllib.error
    health_url = f"{endpoint}/_localstack/health"
    for i in range(max_retries):
        try:
            resp = urllib.request.urlopen(health_url, timeout=2)
            data = json.loads(resp.read())
            if data.get("services", {}).get("lambda") in ("available", "running", "ready"):
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    raise RuntimeError(f"localstack not ready after {max_retries}s at {endpoint}")


def main():
    parser = argparse.ArgumentParser(description='Verify lambpie echo handler on localstack')
    parser.add_argument('--endpoint', default='http://localhost:4577',
                        help='Localstack endpoint URL')
    parser.add_argument('--zip', default=os.path.join(REPO_ROOT, 'target', 'function.zip'),
                        help='Path to function.zip')
    args = parser.parse_args()

    if not os.path.exists(args.zip):
        print(f"Error: {args.zip} not found. Run 'make package' first.")
        sys.exit(1)

    print(f"Waiting for localstack at {args.endpoint}...")
    wait_for_localstack(args.endpoint)
    print("localstack is ready.")

    session = boto3.Session(
        aws_access_key_id='test',
        aws_secret_access_key='test',
        region_name='us-east-1',
    )
    client = session.client('lambda', endpoint_url=args.endpoint)

    # Read the zip
    with open(args.zip, 'rb') as f:
        zip_bytes = f.read()
    print(f"Deploying {FUNCTION_NAME} ({len(zip_bytes):,} bytes)...")

    # Delete existing function if present
    try:
        client.delete_function(FunctionName=FUNCTION_NAME)
    except client.exceptions.ResourceNotFoundException:
        pass

    # Create the function
    client.create_function(
        FunctionName=FUNCTION_NAME,
        Runtime='provided.al2023',
        Role='arn:aws:iam::000000000000:role/lambda-role',
        Handler='bootstrap',
        Code={'ZipFile': zip_bytes},
        MemorySize=128,
        Timeout=30,
    )
    print(f"Function {FUNCTION_NAME} created.")

    # Wait for function to become Active
    print("Waiting for function to become Active...")
    for _ in range(60):
        info = client.get_function(FunctionName=FUNCTION_NAME)
        state = info['Configuration']['State']
        if state == 'Active':
            break
        if state == 'Failed':
            print(f"Function creation failed: {info['Configuration'].get('StateReason', '?')}")
            sys.exit(1)
        time.sleep(1)
    else:
        print(f"Timeout: function still in state {state}")
        sys.exit(1)
    print("Function is Active.")

    # Invoke
    payload = json.dumps(TEST_EVENT).encode()
    print(f"Invoking with: {payload.decode()}")

    response = client.invoke(
        FunctionName=FUNCTION_NAME,
        Payload=payload,
    )

    response_payload = response['Payload'].read()
    status_code = response.get('StatusCode', 0)
    function_error = response.get('FunctionError', '')

    print(f"StatusCode: {status_code}")
    if function_error:
        print(f"FunctionError: {function_error}")
        print(f"Response: {response_payload.decode()}")
        sys.exit(1)

    # Parse response
    response_text = response_payload.decode()
    print(f"Response: {response_text}")

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError:
        print(f"Error: Response is not valid JSON: {response_text}")
        sys.exit(1)

    # Verify echo: response should exactly match the input
    if response_data != TEST_EVENT:
        print(f"FAIL: Expected {TEST_EVENT}, got {response_data}")
        sys.exit(1)

    print("PASS: Echo handler returned the input event exactly.")

    # Cleanup
    client.delete_function(FunctionName=FUNCTION_NAME)
    print("Cleanup complete.")


if __name__ == '__main__':
    main()
