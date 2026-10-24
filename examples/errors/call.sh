#!/bin/sh

# Call the structured error example.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$EXAMPLES_DIR/lib.sh"

heading "Successful validation"
run_protodial \
  --show-status \
  --data '{"resource":"projects/example"}' \
  grpc://localhost:50053/examples.errors.Validator/Validate

heading "Structured validation failure"
if run_protodial \
  --format-error \
  --show-status \
  --data '{}' \
  grpc://localhost:50053/examples.errors.Validator/Validate; then
  echo "expected validation to fail" >&2
  exit 1
fi
