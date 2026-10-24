#!/bin/sh

# Run the documented protodial calls against one or all example servers.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

call_one() {
  name=$1
  script=$EXAMPLES_DIR/$name/call.sh
  if [ ! -x "$script" ]; then
    echo "unknown example: $name" >&2
    return 2
  fi
  "$script"
}

example=${1:-all}
case "$example" in
  all)
    call_one hello
    call_one streaming
    call_one errors
    call_one formats
    call_one deadlines
    ;;
  hello|streaming|errors|formats|deadlines) call_one "$example" ;;
  *)
    echo "usage: $0 {all|hello|streaming|errors|formats|deadlines}" >&2
    exit 2
    ;;
esac
