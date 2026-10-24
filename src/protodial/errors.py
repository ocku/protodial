"""User-facing command-line errors."""

from dataclasses import dataclass

DEPENDENCY_MESSAGE = (
    "Missing dependencies. Reinstall with: python3 -m pip install protodial"
)


class CliError(Exception):
    """An error that should be presented to the user without a traceback."""


class DependencyError(CliError):
    """Required runtime packages are not installed."""


class StdoutBrokenPipe(BrokenPipeError):
    """The CLI's response stream was closed by its downstream consumer."""


class LocalFileError(OSError):
    """A configured local file could not be read.

    Keep the option, path, and original ``OSError`` separate so the CLI can
    render an exact source span without recovering context from prose.
    """

    def __init__(
        self,
        description: str,
        path: str,
        argument: str,
        error: OSError,
        *,
        occurrence: int = 0,
        option_strings: tuple[str, ...] | None = None,
        option_value: str | None = None,
    ) -> None:
        super().__init__(error.errno, error.strerror or str(error), path)
        self.description = description
        self.path = path
        self.argument = argument
        self.original = error
        self.occurrence = occurrence
        self.option_strings = option_strings or (argument,)
        self.option_value = path if option_value is None else option_value


@dataclass(frozen=True)
class TlsMaterialSource:
    """One command-line file value involved in invalid TLS material."""

    description: str
    path: str
    argument: str
    occurrence: int = 0
    option_strings: tuple[str, ...] = ()
    option_value: str | None = None

    def __post_init__(self) -> None:
        if not self.option_strings:
            object.__setattr__(self, "option_strings", (self.argument,))
        if self.option_value is None:
            object.__setattr__(self, "option_value", self.path)


class TlsMaterialError(CliError, ValueError):
    """Configured TLS bytes are empty, malformed, or inconsistent."""

    def __init__(
        self,
        message: str,
        *,
        sources: tuple[TlsMaterialSource, ...],
        reason: str,
        help: str,
        kind: str,
    ) -> None:
        super().__init__(message)
        if not sources:
            raise ValueError("TLS material errors require a source")
        self.sources = sources
        self.reason = reason
        self.help = help
        self.kind = kind


class DescriptorExportError(OSError):
    """A descriptor set could not be exported to its requested destination."""

    def __init__(self, path: str, error: BaseException) -> None:
        error_number = error.errno if isinstance(error, OSError) else None
        error_text = (
            error.strerror
            if isinstance(error, OSError) and error.strerror
            else str(error)
        )
        self.reason = error_text or "descriptor export failed"
        super().__init__(error_number, self.reason, path)
        self.path = path
        self.argument = "--protoset-out"
        self.original = error
        # argparse keeps the final occurrence of scalar options.
        self.occurrence = -1

    def __str__(self) -> str:
        """Describe the cause without exposing an implementation temp path."""
        return self.reason


class LocalOutputError(OSError):
    """A final operation on a local process output stream failed."""

    def __init__(self, stream: str, operation: str, error: OSError) -> None:
        super().__init__(error.errno, error.strerror or str(error))
        self.stream = stream
        self.operation = operation
        self.original = error


class SchemaInputError(CliError):
    """A local schema input could not be decoded or compiled."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        line: int | None = None,
        column: int | None = None,
        reason: str | None = None,
        help: str | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.line = line
        self.column = column
        self.reason = reason
        self.help = help


class ProtoCompilationError(SchemaInputError):
    """A proto source file was rejected by the compiler."""


def add_error_note(error: BaseException, note: str) -> None:
    """Attach a printable secondary-error note on every supported Python."""
    existing = getattr(error, "_protodial_notes", ())
    setattr(error, "_protodial_notes", (*existing, note))
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)


def add_secondary_error(
    primary: BaseException,
    context: str,
    secondary: BaseException,
) -> None:
    """Attach a secondary failure and its existing diagnostics to a primary."""
    add_error_note(primary, f"{context}: {secondary}")
    for note in getattr(secondary, "_protodial_notes", ()):
        add_error_note(primary, f"{context} detail: {note}")
