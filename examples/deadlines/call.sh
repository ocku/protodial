#!/bin/sh

# Demonstrate successful and expired RPC deadlines.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$EXAMPLES_DIR/lib.sh"

heading "Complete within the RPC deadline"
run_protodial \
  --max-time 1 \
  --show-status \
  --data '{"delayMs":25}' \
  grpc://localhost:50055/examples.deadlines.Sleeper/Sleep

heading "Expire an RPC deadline"
if run_protodial \
  --max-time 0.05 \
  --show-status \
  --data '{"delayMs":1000}' \
  grpc://localhost:50055/examples.deadlines.Sleeper/Sleep; then
  echo "expected the deadline to expire" >&2
  exit 1
fi
