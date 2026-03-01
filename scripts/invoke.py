"""Invoke a deployed AWS Lambda function and print the response.

Usage:
    # Basic invocation (uses default echo payload):
    python scripts/invoke.py --function-name my-function

    # Custom payload:
    python scripts/invoke.py --function-name my-function --payload '{"message": "hi", "number": 7}'

    # Against localstack:
    python scripts/invoke.py --function-name my-function --endpoint-url http://localhost:4577

Exit codes:
    0  — successful invocation, response printed to stdout
    1  — invocation error (FunctionError set) or CLI usage error
"""

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

# Default payload used when --payload is not supplied.
# Matches the echo handler's Request type: {message: str, number: int}.
DEFAULT_PAYLOAD = json.dumps({"message": "hello from lambpie", "number": 42})


def main():
    parser = argparse.ArgumentParser(
        description='Invoke a lambpie Lambda function and print the response',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--function-name', required=True,
                        help='Lambda function name or ARN')
    parser.add_argument('--payload', default=DEFAULT_PAYLOAD,
                        help=(
                            'JSON payload to send (default: echo test payload '
                            '{"message": "hello from lambpie", "number": 42})'
                        ))
    parser.add_argument('--region', default=None,
                        help='AWS region (default: from environment / AWS config)')
    parser.add_argument('--endpoint-url', default=None,
                        help='Override endpoint URL (e.g. http://localhost:4577 for localstack)')
    parser.add_argument('--qualifier', default=None,
                        help='Function version or alias to invoke')
    args = parser.parse_args()

    # Validate payload JSON up front so the error is clear.
    try:
        payload_obj = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        sys.exit(f"Error: --payload is not valid JSON: {exc}")

    payload_bytes = json.dumps(payload_obj).encode('utf-8')

    # Build client kwargs — only include keys that are actually set.
    client_kwargs: dict = {}
    if args.region:
        client_kwargs['region_name'] = args.region
    if args.endpoint_url:
        client_kwargs['endpoint_url'] = args.endpoint_url

    client = boto3.client('lambda', **client_kwargs)

    invoke_kwargs: dict = {
        'FunctionName': args.function_name,
        'Payload': payload_bytes,
    }
    if args.qualifier:
        invoke_kwargs['Qualifier'] = args.qualifier

    try:
        response = client.invoke(**invoke_kwargs)
    except ClientError as e:
        sys.exit(f"Error invoking {args.function_name}: {e}")

    http_status = response.get('StatusCode', 0)
    function_error = response.get('FunctionError', '')
    response_payload = response['Payload'].read()

    # Always print the raw response body so the caller can see what happened.
    response_text = response_payload.decode('utf-8')
    print(response_text)

    if function_error:
        # Lambda sets FunctionError to "Handled" or "Unhandled" on errors.
        # Exit non-zero so callers (scripts, CI) detect the failure.
        sys.exit(
            f"Error: Lambda returned FunctionError={function_error!r} "
            f"(HTTP {http_status})"
        )

    if http_status not in (200, 202, 204):
        sys.exit(f"Error: unexpected HTTP status {http_status} from Lambda invoke")


if __name__ == '__main__':
    main()
