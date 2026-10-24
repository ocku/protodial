#!/bin/sh

# Demonstrate JSON, textproto, and raw byte input and output.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$EXAMPLES_DIR/lib.sh"

heading "Generate a request template"
run_protodial describe \
  --template \
  grpc://localhost:50054/examples.formats.Codec/EchoDocument

heading "Echo ProtoJSON with enums, maps, repeated fields, and a oneof"
run_protodial \
  --show-status \
  --data '{"title":"release","content":"aGVsbG8=","tags":["cli","grpc"],"labels":{"stage":"demo"},"priority":"PRIORITY_HIGH","email":"ada@example.com"}' \
  grpc://localhost:50054/examples.formats.Codec/EchoDocument

heading "Echo text protobuf"
run_protodial \
  --input-format text \
  --output-format text \
  --data 'title: "notes" tags: "textproto" priority: PRIORITY_LOW queue_id: 42' \
  grpc://localhost:50054/examples.formats.Codec/EchoDocument

heading "Round-trip raw bytes"
printf '%s' 'raw bytes from stdin' | run_protodial \
  --input-format raw \
  --output-format raw \
  --data @- \
  grpc://localhost:50054/examples.formats.Codec/EchoBlob
printf '\n'
