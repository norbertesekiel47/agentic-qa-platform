.PHONY: help \
	submodule \
	setup-install-dependencies \
	setup-nitro-prepare \
	setup-db-generate \
	setup-db-push \
	setup \
	run \
	unit-test \
	test-with-hurl-and-already-launched-server \
	test-with-hurl \
	test-with-bruno-and-already-launched-server \
	test-with-bruno \
	running-processes-clean \
	non-default-files-clean \
	bruno-generate \
	bruno-check

help:
	@echo "Setup:"
	@echo "  submodule"
	@echo "  setup"
	@echo ""
	@echo "Run:"
	@echo "  run"
	@echo ""
	@echo "Tests:"
	@echo "  unit-test"
	@echo "  test-with-hurl"
	@echo "  test-with-bruno"
	@echo ""
	@echo "Cleaning:"
	@echo "  running-processes-clean"
	@echo "  non-default-files-clean"

########################
# Submodule

submodule:
	git submodule init; git submodule update

########################
# Setup

setup-install-dependencies:
	bun install

setup-nitro-prepare:
	bun run prepare

setup-db-generate:
	bun run db:generate

setup-db-push:
	bun run db:push

setup:
	make setup-install-dependencies
	echo -e '\n\033[0;32m    INSTALLED DEPENDENCIES\033[0m\n'
	make setup-nitro-prepare
	echo -e '\n\033[0;32m    PREPARED NITRO\033[0m\n'
	make setup-db-generate
	echo -e '\n\033[0;32m    GENERATED PRISMA\033[0m\n'
	make setup-db-push
	echo -e '\n\033[0;32m    PUSHED DATABASE SCHEMA\033[0m\n'

########################
# Run

run:  # WARNING clearly not production ready
	JWT_SECRET=$${JWT_SECRET:-conduit-bench-jwt-fixture} bun run dev

########################
# Unit Tests

unit-test:
	bun test

########################
# Tests

test-with-hurl-and-already-launched-server:
	HOST=http://localhost:3000 realworld/specs/api/run-api-tests-hurl.sh

test-with-hurl:
	@set -e; \
	(JWT_SECRET=$${JWT_SECRET:-conduit-bench-jwt-fixture} bun --silent run dev) & \
	SERVER_PID=$$!; \
	trap "kill $$SERVER_PID 2>/dev/null || true" EXIT; \
	sleep 3; \
	kill -0 "$$SERVER_PID" 2>/dev/null || exit 4; \
	make test-with-hurl-and-already-launched-server && ( \
		make running-processes-clean; echo -e '\n\033[0;32m    TESTS OK\033[0m\n' && exit 0 \
	) || ( \
		make running-processes-clean; echo -e '\n\033[0;31m    TESTS FAILED\033[0m\n' && exit 1 \
	)

test-with-bruno-and-already-launched-server:
	HOST=http://localhost:3000 realworld/specs/api/run-api-tests-bruno.sh

test-with-bruno:
	@set -e; \
	(JWT_SECRET=$${JWT_SECRET:-conduit-bench-jwt-fixture} bun --silent run dev) & \
	SERVER_PID=$$!; \
	trap "kill $$SERVER_PID 2>/dev/null || true" EXIT; \
	sleep 3; \
	kill -0 "$$SERVER_PID" 2>/dev/null || exit 4; \
	make test-with-bruno-and-already-launched-server && ( \
		make running-processes-clean; echo -e '\n\033[0;32m    TESTS OK\033[0m\n' && exit 0 \
	) || ( \
		make running-processes-clean; echo -e '\n\033[0;31m    TESTS FAILED\033[0m\n' && exit 1 \
	)

########################
# Cleaning

running-processes-clean:
	ps a -A -o pid,cmd \
	| grep "$$(pwd)" \
	| grep "nitro dev" \
	| grep -v grep \
	| awk '{print $$1}' \
	| xargs -I {} kill {} \
	|| true

non-default-files-clean:
	rm -rf node_modules
	rm -f dev.db
