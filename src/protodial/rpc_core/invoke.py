"""High-level RPC invocation orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..commands.models import Command, InvokeOptions
from ..errors import CliError
from ..formats import MessageFormat
from .call import create_rpc, invoke_descriptor_rpc
from .input import open_input, select_bytes_field
from .models import RpcFailure, RpcSummary
from .protocols import ResponseWriter, RpcCallable

if TYPE_CHECKING:
    from google.protobuf.descriptor import MethodDescriptor
    from google.protobuf.message import Message

    from ..message_io.models import RequestOptions
    from ..schema.context import DescriptorSession
    from ..schema.contracts import GrpcModule


@dataclass(frozen=True)
class _InvocationPlan:
    method: MethodDescriptor
    canonical_method: str
    request_class: type[Message]
    rpc: RpcCallable[Message, Message]
    input_field: str | None
    output_field: str | None
    compression: object | None


def _cardinality(method: MethodDescriptor) -> str:
    request = "stream" if method.client_streaming else "unary"
    response = "stream" if method.server_streaming else "unary"
    return f"{request}-{response}"


def _canonical_method(method: MethodDescriptor) -> str:
    """Return the wire-level, fully qualified ``service/method`` name."""
    return f"{method.containing_service.full_name}/{method.name}"


def _prepare_invocation(
    command: Command,
    grpc: GrpcModule,
    session: DescriptorSession,
    method_name: str,
    options: InvokeOptions,
) -> _InvocationPlan:
    from google.protobuf import message_factory
    from google.protobuf.descriptor import MethodDescriptor

    from ..presentation.diagnostics.models import DiagnosticEvent
    from ..transport_core.options import map_compression

    channel = session.channel
    if channel is None:
        raise RuntimeError("RPC descriptor session did not open a channel")
    method = session.source.find_symbol(method_name)
    if not isinstance(method, MethodDescriptor):
        raise CliError(f"{method_name!r} does not name a method")
    canonical_method = _canonical_method(method)
    session.sink.emit(
        DiagnosticEvent(
            "rpc.resolved",
            level=1,
            fields={
                "method": canonical_method,
                "cardinality": _cardinality(method),
                "request_type": method.input_type.full_name,
                "response_type": method.output_type.full_name,
            },
        )
    )

    request_class = message_factory.GetMessageClass(method.input_type)
    response_class = message_factory.GetMessageClass(method.output_type)
    input_field = (
        select_bytes_field(
            method.input_type, options.input_field, "--input-field"
        )
        if options.input_format == MessageFormat.RAW
        and options.data is not None
        else None
    )
    output_field = (
        select_bytes_field(
            method.output_type, options.output_field, "--output-field"
        )
        if options.output_format == MessageFormat.RAW
        else None
    )
    return _InvocationPlan(
        method,
        canonical_method,
        request_class,
        create_rpc(channel, method, request_class, response_class),
        input_field,
        output_field,
        map_compression(grpc, command.connection.compression),
    )


def _run_invocation(
    command: Command,
    grpc: GrpcModule,
    session: DescriptorSession,
    options: InvokeOptions,
    plan: _InvocationPlan,
    response_writer: ResponseWriter[Message],
) -> RpcSummary:
    from ..message_io.dispatch import iter_requests
    from ..presentation.diagnostics.models import DiagnosticEvent

    session.sink.emit(
        DiagnosticEvent(
            "rpc.started",
            level=2,
            fields={
                "method": plan.canonical_method,
                "compression": command.connection.compression,
            },
            metadata=tuple(session.metadata.rpc),
        )
    )
    with open_input(
        options.data,
        argument=options.data_argument,
        option_strings=options.data_option_strings,
        option_value=options.data_option_value,
    ) as input_source:
        return invoke_descriptor_rpc(
            plan.rpc,
            iter_requests(
                input_source,
                cast("RequestOptions", options),
                plan.method,
                plan.request_class,
                session.source.pool,
                plan.input_field,
            ),
            options,
            command.timeout,
            plan.method,
            session.metadata.rpc,
            session.source.pool,
            plan.output_field,
            grpc,
            command.diagnostics,
            plan.compression,
            response_writer=response_writer,
        )


def invoke(
    command: Command,
    grpc: GrpcModule,
    *,
    response_writer: ResponseWriter[Message],
) -> RpcSummary:
    """Resolve and invoke the RPC described by a command."""
    from ..commands.operations import Invoke
    from ..schema.context import open_descriptor_session

    operation, options = command.operation, command.invoke_options
    if not isinstance(operation, Invoke) or options is None:
        raise TypeError("invoke requires an Invoke operation and options")

    with open_descriptor_session(
        command, grpc, require_channel=True
    ) as session:
        plan = _prepare_invocation(
            command, grpc, session, operation.method, options
        )
        try:
            return _run_invocation(
                command,
                grpc,
                session,
                options,
                plan,
                response_writer,
            )
        except RpcFailure as failure:
            failure.request_type = plan.method.input_type.full_name
            failure.method = plan.canonical_method
            raise
