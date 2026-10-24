# Examples

The examples run real Python gRPC servers with reflection enabled. Each example
contains its protocol, server, Dockerfile, and executable call script.

| Example | Port | Demonstrates |
| --- | ---: | --- |
| `hello` | 50051 | Unary calls, request metadata, headers, and trailers |
| `streaming` | 50052 | Server, client, and bidirectional streaming |
| `errors` | 50053 | Structured `google.rpc.Status` error details |
| `formats` | 50054 | JSON, textproto, raw bytes, and common protobuf fields |
| `deadlines` | 50055 | Successful and expired RPC deadlines |

## Start the servers

Run these commands from the repository root. Docker is the only server-side
requirement.

```console
./examples/run.sh up
./examples/run.sh status
```

The runner builds each image from its own Dockerfile, starts the containers,
and waits for their health checks. Other runner commands are:

```console
./examples/run.sh logs
./examples/run.sh down
```

Runner and server logs use UTC timestamps and stable `event=...` names. Server
RPC completion events include status, message counts where applicable, and
elapsed milliseconds. Arbitrary request metadata is not logged; the hello
example records only its explicitly demonstrated `x-request-id`. Inspect all
server logs with:

```console
./examples/run.sh logs
```

The default server log level is `INFO`. Enable per-message streaming events with
`DEBUG` when starting the containers:

```console
PROTODIAL_EXAMPLE_LOG_LEVEL=DEBUG ./examples/run.sh up
./examples/run.sh logs
```

## Call the servers

Run every call sequence:

```console
./examples/call.sh all
```

The call scripts separate demonstrations with plain `== ... ==` headings.
These headings are not timestamped log records; only `protodial` itself writes
diagnostics, using the human or NDJSON format selected for the command.

Run one example:

```console
./examples/call.sh hello
./examples/call.sh streaming
./examples/call.sh errors
./examples/call.sh formats
./examples/call.sh deadlines
```

The scripts use `protodial` from `PATH`, then `.venv/bin/protodial`, then
`uv run --no-sync protodial`. The commands are also listed in each script and
can be copied individually. Calls request final status diagnostics so their
message counts and elapsed time are visible alongside the response payloads.

Stop all example containers when finished:

```console
./examples/run.sh down
```

The `formats` calls show template generation and round trips using ProtoJSON,
textproto, and raw bytes. The raw response is written directly to stdout. The
`deadlines` calls include an expected `DEADLINE_EXCEEDED` result; its script
handles that nonzero exit as a successful demonstration.
