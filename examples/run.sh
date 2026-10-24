#!/bin/sh

# Build and manage all example gRPC servers.

set -eu

EXAMPLES_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$EXAMPLES_DIR/.." && pwd)
PROJECT_PREFIX=protodial-example
EXAMPLES="hello streaming errors formats deadlines"
EXAMPLE_LOG_LEVEL=${PROTODIAL_EXAMPLE_LOG_LEVEL:-INFO}

runner_log() {
  level=$1
  shift
  printf '%s %-5s [examples] %s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$level" "$*"
}

port_for() {
  case "$1" in
    hello) echo 50051 ;;
    streaming) echo 50052 ;;
    errors) echo 50053 ;;
    formats) echo 50054 ;;
    deadlines) echo 50055 ;;
    *) return 1 ;;
  esac
}

container_for() {
  echo "$PROJECT_PREFIX-$1"
}

image_for() {
  echo "$PROJECT_PREFIX-$1:local"
}

build_one() {
  name=$1
  runner_log INFO "event=image.build_started example=$name"
  docker build \
    --file "$EXAMPLES_DIR/$name/Dockerfile" \
    --tag "$(image_for "$name")" \
    "$REPOSITORY_ROOT"
  runner_log INFO "event=image.build_completed example=$name"
}

start_one() {
  name=$1
  port=$(port_for "$name")
  container=$(container_for "$name")
  docker rm --force "$container" >/dev/null 2>&1 || true
  docker run \
    --detach \
    --env "PROTODIAL_EXAMPLE_LOG_LEVEL=$EXAMPLE_LOG_LEVEL" \
    --rm \
    --name "$container" \
    --publish "$port:$port" \
    "$(image_for "$name")" >/dev/null
  runner_log INFO \
    "event=container.started example=$name container=$container port=$port"
}

wait_one() {
  name=$1
  container=$(container_for "$name")
  attempts=0
  runner_log INFO "event=health.waiting example=$name container=$container"
  while [ "$attempts" -lt 30 ]; do
    status=$(
      docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$container" 2>/dev/null || true
    )
    case "$status" in
      healthy)
        runner_log INFO "event=health.ready example=$name"
        return 0
        ;;
      unhealthy)
        docker logs "$container" >&2 || true
        runner_log ERROR "event=health.failed example=$name status=unhealthy" >&2
        return 1
        ;;
    esac
    attempts=$((attempts + 1))
    sleep 1
  done
  docker logs "$container" >&2 || true
  runner_log ERROR "event=health.timeout example=$name attempts=$attempts" >&2
  return 1
}

up() {
  runner_log INFO "event=environment.starting log_level=$EXAMPLE_LOG_LEVEL"
  for name in $EXAMPLES; do
    build_one "$name"
    start_one "$name"
  done
  for name in $EXAMPLES; do
    wait_one "$name"
  done
  runner_log INFO \
    "event=environment.ready endpoints=localhost:50051,localhost:50052,localhost:50053,localhost:50054,localhost:50055"
}

down() {
  for name in $EXAMPLES; do
    container=$(container_for "$name")
    if docker rm --force "$container" >/dev/null 2>&1; then
      runner_log INFO \
        "event=container.stopped example=$name container=$container"
    else
      runner_log INFO \
        "event=container.absent example=$name container=$container"
    fi
  done
  runner_log INFO "event=environment.stopped"
}

status() {
  printf '%-10s %-18s %s\n' "EXAMPLE" "ENDPOINT" "STATE/HEALTH"
  for name in $EXAMPLES; do
    container=$(container_for "$name")
    port=$(port_for "$name")
    state=$(
      docker inspect \
        --format '{{.State.Status}}/{{.State.Health.Status}}' \
        "$container" 2>/dev/null || echo stopped
    )
    printf '%-10s %-18s %s\n' "$name" "localhost:$port" "$state"
  done
}

logs() {
  for name in $EXAMPLES; do
    container=$(container_for "$name")
    runner_log INFO "event=logs.reading example=$name container=$container"
    if ! docker logs "$container" 2>&1; then
      runner_log WARNING \
        "event=logs.unavailable example=$name container=$container"
    fi
  done
}

command=${1:-up}
case "$command" in
  up) up ;;
  down) down ;;
  status) status ;;
  logs) logs ;;
  *)
    echo "usage: $0 {up|down|status|logs}" >&2
    exit 2
    ;;
esac
