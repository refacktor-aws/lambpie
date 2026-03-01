"""Compile and run the arena stress test inside the Docker build environment.

This script builds a minimal Docker image (reusing the amazonlinux:2023 builder
stage from Dockerfile.build) and runs arena_stress_test.c against the real C
runtime on Linux.  mprotect and fork() are both available there.

Usage:
    python scripts/test_arena.py

Exit code:
    0 — all arena tests passed
    1 — one or more tests failed or Docker is unavailable
"""

import subprocess
import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOCKERFILE_ARENA_TEST = """\
FROM amazonlinux:2023 AS arena-test

RUN dnf install -y gcc make && dnf clean all

WORKDIR /src

COPY runtime/src/arena.h    runtime/src/
COPY runtime/src/arena.c    runtime/src/
COPY runtime/tests/arena_stress_test.c runtime/tests/

RUN gcc -std=c17 -Wall -Wextra -Werror \
        -o /arena_stress_test \
        runtime/tests/arena_stress_test.c \
        runtime/src/arena.c \
        -I runtime/src

CMD ["/arena_stress_test"]
"""


def run(cmd, **kwargs):
    """Run a subprocess; print the command; fail hard on non-zero exit."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"FAILED (exit {result.returncode}): {' '.join(str(c) for c in cmd)}")
        sys.exit(result.returncode)
    return result


def check_docker():
    """Raise immediately if Docker is not available."""
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print("ERROR: Docker is not running or not installed.")
        print("The arena stress test requires Docker (Linux mprotect + fork).")
        sys.exit(1)


def main():
    check_docker()

    # Write a temporary Dockerfile into the repo root so Docker context is correct
    dockerfile_path = os.path.join(REPO_ROOT, "Dockerfile.arena_test")
    try:
        with open(dockerfile_path, "w") as f:
            f.write(DOCKERFILE_ARENA_TEST)

        image_tag = "lambpie-arena-test:latest"

        print("\n[1/2] Building arena-test Docker image...")
        run(
            [
                "docker", "build",
                "--no-cache=false",
                "-f", dockerfile_path,
                "-t", image_tag,
                REPO_ROOT,
            ]
        )

        print("\n[2/2] Running arena stress test...")
        # --rm: clean up container after run
        # --security-opt seccomp=unconfined: some Docker configs suppress SIGSEGV
        #   delivery to child processes via seccomp filter — unconfined lets fork+kill work
        run(
            [
                "docker", "run", "--rm",
                "--security-opt", "seccomp=unconfined",
                image_tag,
            ]
        )

        print("\nAll arena stress tests passed.")

    finally:
        # Always remove the temporary Dockerfile
        if os.path.exists(dockerfile_path):
            os.remove(dockerfile_path)


if __name__ == "__main__":
    main()
