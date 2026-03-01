.PHONY: test ir build package verify clean

# Unit tests (compiler only, runs on host)
test:
	python -m pytest tests/test_compiler.py -v

# Compile .pie to LLVM IR (runs on host, needs llvmlite)
ir:
	python compiler.py tests/echo.pie -o target/echo

# Build bootstrap binary via Docker (full pipeline: .pie -> .ll -> .o -> bootstrap)
build:
	docker build -f Dockerfile.build -o target .

# Package bootstrap into Lambda deployment zip
package: build
	python scripts/package.py

# Full verify: build, start localstack, deploy, invoke, assert, teardown
verify: package
	docker compose up -d --wait localstack
	python scripts/verify.py --endpoint http://localhost:4577 || (docker compose down && exit 1)
	docker compose down

clean:
	rm -rf target/
	docker compose down 2>/dev/null || true
