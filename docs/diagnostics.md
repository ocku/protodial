# Diagnostic output

protodial keeps result data and operational diagnostics on separate streams.
Response messages, discovery results, descriptors, templates, and binary data
are written only to stdout. Warnings, progress, metadata, status, and errors are
written only to stderr. Redirecting either stream never changes its encoding.

Human diagnostics are the default and follow curl's visual grammar. `*` is a
lifecycle, warning, or final-status line; `>` is request metadata; `<` is
response metadata; and fatal errors begin with `protodial: (E####)`. Blocks are
labeled explicitly, and response trailers are identified as the final metadata
sent after the response body. A bare `>` or `<` terminates its metadata block.
`--color auto` colors semantic markers when stderr is a terminal; `always` and
`never` are explicit overrides. Color never applies to stdout or JSON
diagnostics.

RPC failures show structured server details first, followed by the status,
timing, message counts, reason, and canonical method:

```text
ValidateRequest.resource: resource must not be empty
protodial: (E4103) INVALID_ARGUMENT (3) in 760 us; sent 1 message; received 0 messages
  reason: validation failed
  call: `example.Validator/Validate`
```

Even when the command uses a unique suffix such as `Validator/Validate`, RPC
events report the resolved, fully qualified `package.Service/Method`. Human
failures omit the network target to keep the call context compact; schema-v1
JSON retains `context.target` for automation.

Diagnostics are limited to 65,536 characters. Human output prioritizes the
headline, source highlights, remediation, and corrective details such as `BadRequest`,
`PreconditionFailure`, and `QuotaFailure` over metadata and debug information.
A truncation note reports the number of omitted characters.

## Request input errors

Request parsing errors use `input.invalid` with code `E2101`. Malformed JSON
reports that the request body is not valid JSON, with a source location when
available. Valid JSON that cannot be mapped to the request message instead
names the expected ProtoJSON schema and gives a field or value failure reason.

For a ProtoJSON schema mismatch, JSON diagnostics additionally include
`context.kind: "protojson_schema"`, `context.request_type` (the fully qualified
protobuf message name), and `context.request_index` (one-based within the
request stream). These fields distinguish schema mismatches without parsing
human wording. Both cases retain exit code 1.

## NDJSON schema v1

`--diagnostic-format json` writes one compact JSON object per stderr line and
flushes every event.

Each object has these required fields:

- `schema`: always `protodial.diagnostic.v1`;
- `sequence`: a one-based, increasing integer for this process;
- `severity`: `error`, `warning`, `info`, or `status`;
- `event`: a stable dotted event name;
- `message`: concise plain text with no ANSI styling.

Optional fields are omitted when empty:

- `code`: a stable `E####` or `W####` category;
- `context`: typed event-specific values;
- `spans`: source, one-based line/column, display text, offsets, and label;
- `causes`, `help`, and `notes`: ordered strings;
- `metadata`: an ordered array that retains duplicate keys;
- `details`: structured rich-status details.

Binary values use `{"encoding":"base64","data":"..."}`. Non-finite numbers
are rejected. No event contains a wall-clock timestamp; `sequence` provides
ordering and relevant durations use numeric seconds in context.

Untruncated `details` always retain their original wire order. If the diagnostic
limit requires truncation, protodial retains the highest-priority
whole details that fit, in their original relative order. A final
`protodial.truncated` detail records `omitted_detail_indexes` in wire order,
`original_detail_count`, an optional serialized preview, and the exact number
of omitted preview characters. These are additive fields within schema v1.

`rpc.completed` and `rpc.failed` expose the same additive terminal context:
`status` is the status name, `grpc_status` is its name/number object, and
`sent`/`received` are message counters. For schema-v1 compatibility, the
existing `sent_messages`/`received_messages` spellings are also emitted, and
the existing fields retain their original types. Consumers should accept
either counter spelling while older producers remain in use.

Example:

```json
{"schema":"protodial.diagnostic.v1","sequence":1,"severity":"error","event":"rpc.failed","code":"E4103","message":"RPC `example.Users/Create` failed with INVALID_ARGUMENT (3)","context":{"target":"api.example.com:443","method":"example.Users/Create","request_type":"example.CreateUserRequest","status":"INVALID_ARGUMENT","grpc_status":{"name":"INVALID_ARGUMENT","number":3},"elapsed_seconds":0.0241,"sent":1,"received":0,"sent_messages":1,"received_messages":0},"causes":["request validation failed"]}
```

Within schema v1, new event names and optional fields may be added. Removing a
field or changing an existing field's type requires a new schema identifier.
Human wording and styling are not an automation interface.

## Security

Human rendering escapes terminal controls, carriage returns, line separators,
and bidirectional formatting controls before adding ANSI styling.
