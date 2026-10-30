"""Command-line entry point."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from .argument_parser import parse_arguments
from .argument_parser.diagnostics import (
    argument_span as _argument_span,
)
from .argument_parser.diagnostics import (
    bootstrap_policy as _bootstrap_policy,
)
from .argument_parser.diagnostics import (
    bootstrap_policy_details as _bootstrap_policy_details,
)
from .argument_parser.diagnostics import (
    parser_message as _parser_message,
)
from .commands.models import Command
from .commands.operations import Invoke, ListSymbols
from .errors import (
    CliError,
    DependencyError,
    LocalOutputError,
    StdoutBrokenPipe,
    error_notes,
)
from .presentation.diagnostics.context import use_sink
from .presentation.diagnostics.error_events import ordinary_error_event
from .presentation.diagnostics.models import (
    DiagnosticEvent,
    DiagnosticSeverity,
    DiagnosticValue,
)
from .presentation.diagnostics.rendering import StderrDiagnosticSink
from .presentation.rpc import RpcError, status_parts
from .schema.paths import ensure_default_proto_paths

if TYPE_CHECKING:
    from .rpc_core.models import RpcFailure
    from .rpc_core.runtime import Runtime
    from .schema.contracts import GrpcModule


def load_runtime() -> Runtime:
    """Load the RPC runtime after the diagnostic policy is available."""
    from .rpc_core.runtime import load_runtime as load

    return load()


def invoke(command: Command, grpc: GrpcModule) -> object:
    """Invoke lazily so missing dependencies reach the CLI boundary."""
    from .rpc import invoke as run

    return run(command, grpc)


def list_symbols(command: Command, grpc: GrpcModule) -> None:
    """List lazily so missing dependencies reach the CLI boundary."""
    from .rpc import list_symbols as run

    run(command, grpc)


def describe(command: Command, grpc: GrpcModule) -> None:
    """Describe lazily so missing dependencies reach the CLI boundary."""
    from .rpc import describe as run

    run(command, grpc)


def _replace_stream_with_devnull(name: str) -> None:
    """Replace an unusable standard stream when fd redirection is impossible."""
    try:
        stream = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return
    setattr(sys, name, stream)


def _silence_broken_stream(name: str) -> None:
    """Keep an unusable output stream from failing interpreter shutdown."""
    try:
        stream_fd = getattr(sys, name).fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        _replace_stream_with_devnull(name)
        return
    redirected = False
    try:
        os.dup2(devnull_fd, stream_fd)
        redirected = True
    except OSError:
        pass
    finally:
        if devnull_fd != stream_fd:
            os.close(devnull_fd)
    if not redirected:
        _replace_stream_with_devnull(name)


def _silence_broken_stdout() -> None:
    _silence_broken_stream("stdout")


def _silence_broken_stderr() -> None:
    _silence_broken_stream("stderr")


def _emit(sink: StderrDiagnosticSink, event: DiagnosticEvent) -> bool:
    """Emit while containing failures of the diagnostic stream itself."""
    try:
        sink.emit(event)
    except BrokenPipeError:
        _silence_broken_stderr()
        return False
    except OSError:
        _silence_broken_stderr()
        return False
    return True


def _parse_with_diagnostics(
    argv: Sequence[str], sink: StderrDiagnosticSink
) -> tuple[Command | None, int | None]:
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            command = parse_arguments(argv)
    except BrokenPipeError as error:
        # argparse only writes to stdout here for --help and --version.
        raise StdoutBrokenPipe from error
    except SystemExit as error:
        code = int(error.code) if isinstance(error.code, int) else 1
        if code == 2:
            message = _parser_message(captured.getvalue())
            spans = _argument_span(argv, message)
            _emit(
                sink,
                DiagnosticEvent(
                    "usage.invalid",
                    message,
                    level=0,
                    severity=DiagnosticSeverity.ERROR,
                    code="E1001",
                    spans=spans,
                    help=("run `protodial --help` for usage",),
                ),
            )
        return None, code
    return command, None


def _rich_details(status: object) -> tuple[DiagnosticValue, ...]:
    from .rich_status.rendering import diagnostic_details

    return diagnostic_details(getattr(status, "details", ()))


def _rpc_failure_event(
    command: Command,
    failure: RpcFailure,
) -> DiagnosticEvent:
    summary = failure.summary
    name, number = status_parts(summary.code)
    method = failure.method or (
        command.operation.method
        if isinstance(command.operation, Invoke)
        else "unknown"
    )
    code_text = f"{name} ({number})" if number is not None else name
    fields: dict[str, DiagnosticValue] = {
        "target": command.target.channel,
        "method": method,
        "status": name,
        "grpc_status": {"name": name, "number": number},
        "elapsed_seconds": summary.elapsed_seconds,
        "sent": summary.sent_messages,
        "received": summary.received_messages,
        "sent_messages": summary.sent_messages,
        "received_messages": summary.received_messages,
    }
    if failure.request_type is not None:
        fields["request_type"] = failure.request_type
    notes = list(error_notes(failure))
    rich = failure.rich_status
    details: tuple[DiagnosticValue, ...] = ()
    if rich is not None:
        details = _rich_details(rich)
        notes.extend(str(item) for item in rich.warnings)
        if rich.decoded and rich.code is not None and rich.code != number:
            fields["rich_status"] = {
                "name": _rich_code_name(rich.code),
                "number": rich.code,
            }
    return DiagnosticEvent(
        "rpc.failed",
        f"RPC `{method}` failed with {code_text}",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields=fields,
        causes=(summary.details,) if summary.details else (),
        notes=tuple(notes),
        details=details,
    )


def _rich_code_name(number: int) -> str:
    from .rich_status.decoding import CODE_NAMES

    return CODE_NAMES.get(number, "UNKNOWN")


def _grpc_error_event(command: Command, error: RpcError) -> DiagnosticEvent:
    name, number = status_parts(error.code())
    method = (
        command.operation.method
        if isinstance(command.operation, Invoke)
        else "unknown"
    )
    code_text = f"{name} ({number})" if number is not None else name
    return DiagnosticEvent(
        "rpc.failed",
        f"RPC `{method}` failed with {code_text}",
        level=0,
        severity=DiagnosticSeverity.ERROR,
        code="E4103",
        fields={
            "target": command.target.channel,
            "method": method,
            "status": name,
            "grpc_status": {"name": name, "number": number},
        },
        causes=(str(error.details()),) if error.details() else (),
        notes=error_notes(cast(BaseException, error)),
    )


def _run(
    command: Command,
    sink: StderrDiagnosticSink,
    argv: Sequence[str] = (),
) -> int:
    from .rpc_core.models import RpcFailure

    try:
        ensure_default_proto_paths()
        runtime = load_runtime()
    except DependencyError as error:
        _emit(sink, ordinary_error_event(error, command, argv))
        return 1
    except StdoutBrokenPipe:
        _silence_broken_stdout()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _emit(sink, ordinary_error_event(error, command, argv))
        return 1

    try:
        if isinstance(command.operation, Invoke):
            invoke(command, runtime.grpc)
        elif isinstance(command.operation, ListSymbols):
            list_symbols(command, runtime.grpc)
        else:
            describe(command, runtime.grpc)
        return 0
    except RpcFailure as failure:
        _emit(sink, _rpc_failure_event(command, failure))
        return 1
    except StdoutBrokenPipe:
        _silence_broken_stdout()
        return 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError as error:
        _emit(sink, ordinary_error_event(error, command, argv))
        return 1
    except runtime.grpc.RpcError as error:
        _emit(sink, _grpc_error_event(command, cast(RpcError, error)))
        return 1
    except (CliError, OSError, *runtime.protobuf_errors) as error:
        _emit(sink, ordinary_error_event(error, command, argv))
        return 1
    except Exception as error:
        _emit(sink, ordinary_error_event(error, command, argv))
        return 1


def _main(argv: Sequence[str] | None = None) -> int:
    """Run one command before the process-level stdout boundary."""
    # grpcio's C core writes directly to fd 2 and bypasses the structured
    # diagnostic sink. Resolve this before the lazy grpc import in every mode.
    os.environ["GRPC_VERBOSITY"] = "NONE"
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    completion_actions = {"bash", "zsh", "install", "-h", "--help"}
    if arguments[:1] == ("completion",) and (
        len(arguments) == 1 or arguments[1] in completion_actions
    ):
        from .completion import run_completion_command

        try:
            return run_completion_command(arguments[1:])
        except SystemExit as error:
            return int(error.code) if isinstance(error.code, int) else 1
    if arguments[:2] == ("__complete", "--protocol=protodial-v1"):
        from .completion import run_completion_protocol

        return run_completion_protocol(arguments[1:])
    (
        diagnostic_format,
        color,
        bootstrap_error,
        bootstrap_span,
    ) = _bootstrap_policy_details(arguments, os.environ)
    bootstrap_sink = StderrDiagnosticSink(
        0,
        diagnostic_format=diagnostic_format,
        color=color,
    )
    if bootstrap_error is not None:
        _emit(
            bootstrap_sink,
            DiagnosticEvent(
                "usage.invalid_diagnostic_format",
                bootstrap_error,
                level=0,
                severity=DiagnosticSeverity.ERROR,
                code="E1002",
                spans=(bootstrap_span,) if bootstrap_span is not None else (),
                help=(
                    "use `--diagnostic-format human` or "
                    "`--diagnostic-format json`",
                ),
            ),
        )
        return 2
    command, parse_exit = _parse_with_diagnostics(arguments, bootstrap_sink)
    if command is None:
        return 1 if parse_exit is None else parse_exit
    sink = StderrDiagnosticSink(
        command.diagnostics.verbosity,
        diagnostic_format=command.diagnostics.diagnostic_format,
        color=command.diagnostics.color,
    )
    with use_sink(sink):
        return _run(command, sink, arguments)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command, treating a closed stdout consumer as success."""
    exit_code: int | None = None
    try:
        exit_code = _main(argv)
        # Flush while the broken-pipe boundary is still active. Otherwise a
        # buffered help, version, discovery, or response write can fail during
        # interpreter shutdown and leak an ignored-exception traceback.
        sys.stdout.flush()
        return exit_code
    except StdoutBrokenPipe:
        _silence_broken_stdout()
        return exit_code if exit_code else 0
    except BrokenPipeError:
        # This boundary surrounds only the explicit stdout flush above. Other
        # broken pipes are handled at their known output sites.
        _silence_broken_stdout()
        return exit_code if exit_code else 0
    except OSError as error:
        _silence_broken_stdout()
        if error.errno == errno.EPIPE:
            return exit_code if exit_code else 0
        arguments = tuple(sys.argv[1:] if argv is None else argv)
        diagnostic_format, color, _ = _bootstrap_policy(arguments, os.environ)
        sink = StderrDiagnosticSink(
            0,
            diagnostic_format=diagnostic_format,
            color=color,
        )
        output_error = LocalOutputError("standard output", "flush", error)
        _emit(sink, ordinary_error_event(output_error))
        return exit_code if exit_code else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
