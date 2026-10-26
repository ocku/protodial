# protodial

A Python command-line client for gRPC. Call methods, list services, inspect
protobuf definitions, and generate request templates using server reflection,
local `.proto` files, or descriptor sets. No generated client code is required.

Supports unary, client-streaming, server-streaming, and bidirectional RPCs.
Requests and responses can use ProtoJSON, textproto, serialized protobuf, or
raw bytes. Response payloads go to stdout; diagnostics go to stderr.

## Installation

Requires Python 3.10 or newer. Install the checkout with
[uv](https://docs.astral.sh/uv/):

```console
uv tool install .
protodial --version
```

If the package is available on your package index, install version 1.1.2 with
`uv tool install protodial==1.1.2` or `python -m pip install protodial==1.1.2`.
You can also run it as `python -m protodial`.

### Shell completion

Install and activate completion in the current Bash or Zsh (detected from
`$SHELL`) with one sourceable command:

```console
source "$(protodial completion install)"
```

Use `--shell bash` or `--shell zsh` if detection fails, or `--path FILE` to choose
the installed file. Repeated installs update protodial-generated files;
replacing other files requires `--force`. The command never edits shell startup
files.

For future Bash sessions, load the bash-completion framework. For Zsh, the
installer prefers configured Oh My Zsh or exported `FPATH` directories. If it
uses the XDG fallback, configure `~/.zshrc` as follows:

```zsh
fpath=("${XDG_DATA_HOME:-$HOME/.local/share}/zsh/site-functions" $fpath)
autoload -Uz compinit
compinit
```

Alternatively, add the `source` command above to your shell startup file to
install or refresh and activate completion on each startup.

To load completion without installing it:

```console
# Bash
source <(protodial completion bash)

# Zsh
source <(protodial completion zsh)
```

Completion covers commands, options, paths, services, methods, schema symbols,
fields, and enum or boolean values. Methods come from explicit schemas,
[reusable protos](#local-schemas-and-descriptor-sets), or server reflection.
Reusable protos provide suggestions without becoming implicit RPC inputs.

Filesystem and reflection work is bounded to keep completion responsive.
`PROTODIAL_COMPLETION_TIMEOUT` overrides the default 0.2-second reflection
deadline with a positive number of seconds.

## Quick start

Call a server with [reflection](https://grpc.io/docs/guides/reflection/) enabled:

```console
protodial -d '{"name":"Ada"}' \
  grpc://localhost:50051/example.Greeter/SayHello
```

The [hello example](examples/README.md) returns compact ProtoJSON:

```json
{"message":"Hello, Ada!","requestId":"example-123"}
```

Use `grpc://` for plaintext and `grpcs://` for TLS. Bare targets and gRPC
resolver URIs use TLS by default. For resolver or Unix-socket targets, specify
`--method` separately:

```console
protodial --plaintext --method example.Greeter/SayHello dns:///localhost:50051
protodial --plaintext --method example.Greeter/SayHello unix:///tmp/example.sock
```

When reflection is unavailable, provide a schema:

```console
protodial --proto api.proto --proto-path protos -d @request.json \
  grpcs://api.example.com:443/example.Greeter/SayHello
```

Invocation options precede the target. Discovery options follow `list` or
`describe`. Run `protodial --help`, `protodial list --help`, or
`protodial describe --help` for the full option reference. Run
`protodial completion --help` for completion installation options.

## Schema discovery

```console
# List services, then methods on a service.
protodial list grpc://localhost:50051
protodial list grpc://localhost:50051 example.Greeter

# Inspect a service, method, message, or field.
protodial describe grpc://localhost:50051 example.Greeter
protodial describe grpc://localhost:50051/example.Greeter/SayHello
protodial describe grpc://localhost:50051 example.HelloRequest.name

# Generate a ProtoJSON request template.
protodial describe --template \
  grpc://localhost:50051/example.Greeter/SayHello
```

Service names are sorted; methods retain declaration order. Reflection services
are hidden unless `--show-reflection` is set. Unique short or suffix names are
accepted; ambiguous names report the qualified candidates.

`describe --template` accepts a message or method. For methods, it uses the
request type. Templates include one repeated item and the first alternative
of each oneof. `--template-depth` limits nesting (default: 8).

### Local schemas and descriptor sets

```console
protodial list --proto api.proto --proto-path protos unused.example:443
protodial describe --protoset api.protoset \
  unused.example:443 example.HelloRequest
protodial list --protoset-out schema.pb grpc://localhost:50051
```

- Repeat `--proto` to compile multiple entrypoints or `--protoset` to combine
  binary `FileDescriptorSet` files. Conflicting definitions of the same file
  are rejected.
- Local schemas disable reflection by default. Add `--use-reflection` to resolve
  missing symbols from the server.
- Local-only discovery does not contact the target, but still requires a target
  operand.
- `--protoset-out` atomically exports the resolved schema and its dependencies.
- Repeat `--proto-path` for import roots. The default reusable schema directory
  is `$XDG_DATA_HOME/protodial/protos`, falling back to
  `$HOME/.local/share/protodial/protos`. Invocation, `list`, and `describe`
  create the default directory automatically. Custom import roots must already
  exist; help, version, and shell completion do not create proto directories.

Invocation URL paths use `/SERVICE/METHOD`. Discovery URLs can contain
`/SERVICE` for `list`, or `/SYMBOL` or `/SERVICE/METHOD` for `describe`.
URL credentials, query strings, and fragments are not supported.

## Request and response formats

Select encodings independently with `--input-format` and `--output-format`.
Both default to `json`.

| Format | Unary | Streaming |
| --- | --- | --- |
| `json` | One ProtoJSON value | Whitespace-separated values; output is NDJSON |
| `text` | One textproto document | Documents separated by a line containing `---` |
| `protobuf` | One serialized message | Four-byte unsigned big-endian length, then message bytes |
| `raw` | One top-level `bytes` field | Input byte chunks; concatenated output bytes |

Protobuf file framing is a protodial convention, not gRPC wire framing.
ProtoJSON `bytes` fields use base64 strings.

`-d` / `--data` accepts a literal value, `@path` for a file, or `-` / `@-` for
stdin. Without `--data`, unary calls send one empty message and client streams
send no messages, regardless of format.

JSON client streams are parsed incrementally. NDJSON, multiline values, and
consecutive whitespace-separated values are accepted. Responses are compact
JSON values, one per line:

```console
printf '%s\n' '{"value":1}' '{"value":2}' | \
  protodial -d @- grpc://localhost:50051/example.Service/Upload

protodial -d '{"name":"Ada"}' \
  grpc://localhost:50051/example.Greeter/SayHello | jq .
```

Use `--allow-unknown-fields` to ignore unknown JSON input fields,
`--emit-defaults` to include default-valued output fields, and
`--max-input-buffer` to limit buffered parser input.

### Raw file input

Bind a file to a `bytes` field with `--field:file`; this selects raw input.
Use repeatable `--field` options for other top-level fields:

```console
protodial --field filename=video.bin --field:file content=video.bin \
  grpc://localhost:50051/example.Service/Upload
```

`--field` values are parsed as ProtoJSON values, or as strings if they are not
valid JSON. `--field:file content=-` reads bytes from stdin.

With `--input-format raw` or `--output-format raw`, the bytes field is inferred
if there is exactly one eligible field. Otherwise, select it with
`--input-field` or `--output-field`. Client-streaming raw input is split into
`--chunk-size` byte messages; `--field` values apply to every message.

Server streams flush each response as it arrives. Bidirectional calls send and
receive concurrently. Failed streams preserve partial stdout output and exit
nonzero. Ctrl-C and a closed output pipe cancel the active RPC. Interactive
prompting is not supported; use `-d @-` for streaming stdin.

## Metadata and diagnostics

```console
protodial -H 'authorization:Bearer token' --rpc-header 'x-request-id:1234' \
  -i --show-trailers --show-status \
  grpcs://api.example.com:443/example.Service/Call
```

`-H` / `--header` metadata is sent to reflection and the application RPC.
`--reflect-header` and `--rpc-header` add scope-specific metadata without
replacing duplicate keys. Values for keys ending in `-bin` must be base64.

Select diagnostics with:

- `-i` / `--include`: response headers.
- `--show-trailers`: response trailers.
- `--show-status`: final status, message counts, and elapsed time.
- `--format-error`: decode `google.rpc.Status` and its detail messages.
- `-v`: response diagnostics, structured errors, and resolved method.
- `-vv`: also include connection, transport, schema, and RPC lifecycle events.

Human diagnostics use `*` for status, `>` for request metadata, and `<` for
response metadata. `--color auto|always|never` controls stderr color. Automatic
color requires a terminal and honors `NO_COLOR` and `TERM=dumb`.

For scripts, use NDJSON diagnostics rather than parsing human output:

```console
protodial -vv --diagnostic-format json \
  grpc://localhost:50051/example.Greeter/SayHello \
  >responses.ndjson 2>diagnostics.ndjson
```

`PROTODIAL_DIAGNOSTIC_FORMAT=human|json` sets the default; the CLI option takes
precedence. See [the diagnostic schema](docs/diagnostics.md) for the
`protodial.diagnostic.v1` contract.

## TLS and connection controls

| Option | Purpose |
| --- | --- |
| `--cacert FILE` | Trust a PEM CA bundle; repeatable |
| `--cert FILE --key FILE` | Present a client certificate and private key |
| `--key-password PASSWORD` | Decrypt an encrypted private key |
| `--servername NAME` | Set TLS SNI and the verified server identity |
| `--authority NAME` | Set HTTP/2 `:authority` |
| `-k` / `--insecure` | Disable TLS peer verification; unsafe |

`--cert` and `--key` are required together. Certificate options cannot be used
with `--plaintext`. A key password passed on the command line may be exposed
in shell history and the process list.

`--insecure` uses a local TLS relay and always emits a warning. On Unix, the
relay uses an owner-only temporary directory and a socket with mode `0600`.
The loopback TCP fallback on platforms without Unix sockets rejects client
certificates to prevent other local processes from using that identity.
Neither mode protects against remote man-in-the-middle attacks.

`--connect-timeout` bounds the channel-readiness check before schema discovery
or invocation. `-m` / `--max-time` sets the RPC deadline in seconds.
`--wait-for-ready` lets an RPC wait for an available channel within that
deadline; local-schema calls with this flag skip the readiness preflight.
Reflection still requires the preflight.

`--max-send-message-size` and `--max-recv-message-size` set gRPC message limits.
`--compression` accepts `none`, `gzip`, or `deflate`. `--keepalive-time` sets the
HTTP/2 keepalive interval in seconds; aggressive intervals may be rejected by
servers. `-A` / `--user-agent` sets the primary gRPC user agent.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `1` | Dependency, RPC, input, schema, or file error |
| `2` | Invalid CLI usage |
| `130` | Interrupted with Ctrl-C |

## Docker

```console
docker build -t protodial:1.1.2 .
docker run --rm protodial:1.1.2 --version
docker run --rm protodial:1.1.2 -d '{"name":"Ada"}' \
  grpc://host.docker.internal:50051/example.Greeter/SayHello
```

On Docker Desktop, `host.docker.internal` addresses the host. On Linux, use
`--network host` with a localhost target when appropriate. Mount local schema,
request, and certificate files into the container; add `-i` when reading stdin.

## Development

```console
uv sync --dev
uv run protodial --help
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv build
```

CI runs formatting, linting, type checking, and tests on Linux, macOS, and
Windows with Python 3.10 and 3.14, plus wheel and Docker smoke checks.

See [examples](examples/README.md) for runnable servers and calls, and
[CHANGELOG.md](CHANGELOG.md) for release notes. The CLI is the primary supported
interface; this README does not define a stable Python library API.
