# Changelog

## Unreleased

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
