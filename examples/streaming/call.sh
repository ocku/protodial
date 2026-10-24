#!/bin/sh

# Call all streaming shapes exposed by the streaming example.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$EXAMPLES_DIR/lib.sh"

heading "Server streaming"
run_protodial \
  --show-status \
  --data '{"text":"echo","count":3}' \
  grpc://localhost:50052/examples.streaming.Streamer/Expand

heading "Client streaming"
run_protodial \
  --show-status \
  --data '{"text":"one"} {"text":"two"} {"text":"three"}' \
  grpc://localhost:50052/examples.streaming.Streamer/Collect

heading "Bidirectional streaming"
run_protodial \
  --show-status \
  --data '{"text":"hello"} {"text":"world"}' \
  grpc://localhost:50052/examples.streaming.Streamer/Chat
