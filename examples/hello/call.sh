#!/bin/sh

# Call the unary hello example.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$EXAMPLES_DIR/lib.sh"

heading "List reflected services"
run_protodial list grpc://localhost:50051

heading "Describe the Greeter service"
run_protodial describe grpc://localhost:50051 examples.hello.Greeter

heading "Call SayHello with request metadata"
run_protodial \
  --header 'x-request-id:example-123' \
  --include \
  --show-status \
  --show-trailers \
  --data '{"name":"Ada"}' \
  grpc://localhost:50051/examples.hello.Greeter/SayHello
