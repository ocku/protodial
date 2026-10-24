#!/bin/sh

# Shared shell helpers for the example call scripts.

set -eu

if [ -z "${EXAMPLES_DIR:-}" ]; then
  EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
REPOSITORY_ROOT=$(CDPATH= cd -- "$EXAMPLES_DIR/.." && pwd)

run_protodial() {
  if command -v protodial >/dev/null 2>&1; then
    protodial "$@"
  elif [ -x "$REPOSITORY_ROOT/.venv/bin/protodial" ]; then
    "$REPOSITORY_ROOT/.venv/bin/protodial" "$@"
  elif command -v uv >/dev/null 2>&1; then
    (
      cd "$REPOSITORY_ROOT"
      uv run --no-sync protodial "$@"
    )
  else
    echo "protodial is not installed; run 'uv sync --dev' first" >&2
    return 127
  fi
}

heading() {
  printf '\n== %s ==\n' "$1"
}
