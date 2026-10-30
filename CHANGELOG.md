# Changelog

## Unreleased

## 1.1.3 — 2026-10-30

- Fix directory completion after a trailing separator, respect the supplied
  working directory when completing installation paths, and discover fields
  after compact nested declarations, proto2 groups, and hexadecimal field
  numbers in saved schemas.
- Preserve enum-value options and group bodies in symbol descriptions, and
  render RPC method options using valid protobuf syntax verified by `protoc`.
- Preserve interrupts and request-producer errors when RPC cancellation or
  terminal diagnostics also fail, without cancelling the same call twice.
- Split CLI, schema, RPC, transport, diagnostics, and test modules by
  responsibility. Consolidate shared types, parser metadata, diagnostic
  choices, exception notes, and completion logic. Document generated-file
  exceptions to the 500-line limit.
- Test non-editable installations in CI and run every Docker example against
  the installed client. Use installed resources in subprocess and shell tests,
  remove superficial example assertions, and keep type-contract fixtures under
  `tests/typing`.
- Correct the documented JSON response and fresh-install test instructions,
  and avoid leaving a root-owned build cache in the Docker user's home.

## 1.1.2 — 2026-10-26

- Create the default reusable proto directory automatically when invoking an
  RPC or running `list` or `describe`.
- Stabilize in-process CLI output assertions against asynchronous grpcio native
  shutdown logs, and use controlled clocks for completion deadline unit tests.
- Document persistent shell completion setup, ProtoJSON schema diagnostics, and
  the current development checks.

## 1.1.1 — 2026-10-26

- Make completion installation idempotent and install Zsh completions into a
  configured completion directory when available, so fresh shells discover
  them without a manual `source` command.
- Show one long-form row per CLI option with its short alias and description,
  and keep values visible alongside context for methods and all other
  completion candidates.
- Distinguish malformed JSON from valid JSON that does not match the RPC's
  ProtoJSON request schema, naming the expected protobuf message type and
  keeping the diagnostic and its reason on readable single lines.

## 1.1.0 — 2026-10-25

- Add installable Bash and Zsh completion for commands, options, proto files,
  reflected and local methods, schema symbols, fields, and enum values. Shell
  scripts can be generated directly or installed to an XDG data directory.
- Discover reusable protos through a bounded, stateless in-memory scan and
  compile only the selected schema when descriptor details are needed.
- Keep interactive completion responsive with short filesystem, compiler, and
  server-reflection deadlines; unavailable reflection endpoints return in
  roughly 0.3 seconds rather than waiting for normal RPC timeouts.

## 1.0.2 — 2026-10-25

- Fix insecure TLS relay I/O under concurrent traffic and high file-descriptor
  counts.
- Fix Windows handling of local proto imports and stdin shutdown after a
  streaming RPC completes.

## 1.0.1 — 2026-10-25

- Remove `--expand-headers`; use shell expansion for environment variables in
  metadata values instead.

## 1.0.0 — 2026-10-25

Initial release.

- Invoke unary, client-streaming, server-streaming, and bidirectional gRPC methods.
- Discover services and protobuf symbols through server reflection, local
  `.proto` files, or binary descriptor sets. Generate request templates and
  export resolved descriptor sets.
- Read and write ProtoJSON, textproto, serialized protobuf, and raw bytes.
  Stream requests from stdin and bind files to protobuf bytes fields.
- Configure TLS, custom CAs, mutual TLS, server identity, and HTTP/2 authority.
  Support plaintext and explicit unverified TLS connections.
- Send shared or scoped request metadata. Inspect response headers, trailers,
  status, and structured gRPC errors.
- Emit human-readable or versioned NDJSON diagnostics on stderr while keeping
  response payloads on stdout.
- Configure deadlines, readiness checks, message limits, compression, and
  keepalives. Support resolver URIs and Unix-socket targets.
- Provide a Python 3.10+ package, a `protodial` command, and a Docker image build.
