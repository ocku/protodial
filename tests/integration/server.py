from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent import futures
from threading import Lock
from typing import Any

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.rpc import error_details_pb2, status_pb2
from grpc_reflection.v1alpha import reflection, reflection_pb2

from .support import SERVICE_NAME, RunningServer, Schema


class MetadataRecorder(grpc.ServerInterceptor):
    def __init__(
        self, records: list[tuple[str, tuple[tuple[str, str | bytes], ...]]]
    ) -> None:
        self._records = records
        self._lock = Lock()

    def intercept_service(
        self,
        continuation: Any,
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        metadata = tuple(
            (item.key, item.value)
            for item in handler_call_details.invocation_metadata
        )
        with self._lock:
            self._records.append((handler_call_details.method, metadata))
        return continuation(handler_call_details)


def _handlers(schema: Schema) -> dict[str, grpc.RpcMethodHandler]:
    payload = schema.payload
    text_only = schema.text_only

    def prepare(context: grpc.ServicerContext) -> None:
        context.send_initial_metadata((("e2e-header", "ready"),))
        context.set_trailing_metadata((("e2e-trailer", "done"),))

    def unary(request: Any, context: grpc.ServicerContext) -> Any:
        prepare(context)
        if request.text == "large":
            return payload(text="large", data=b"x" * 1024)
        return payload(text=request.text.upper(), data=request.data)

    def server_stream(
        request: Any, context: grpc.ServicerContext
    ) -> Iterator[Any]:
        prepare(context)
        yield payload(text=f"{request.text}-1")
        yield payload(text=f"{request.text}-2")

    def unary_text_only(request: Any, context: grpc.ServicerContext) -> Any:
        prepare(context)
        return payload(text=request.text.upper())

    def client_stream(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        prepare(context)
        return payload(text="|".join(request.text for request in requests))

    def client_stream_text_only(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        prepare(context)
        return payload(text="|".join(request.text for request in requests))

    def bidi(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        prepare(context)
        for request in requests:
            yield payload(text=request.text.upper())

    def terminal_client_stream(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Any:
        next(requests)
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "stream rejected")

    def rich_error(_request: Any, context: grpc.ServicerContext) -> Any:
        detail = error_details_pb2.BadRequest()
        detail.field_violations.add(field="text", description="must be useful")
        status = status_pb2.Status(
            code=grpc.StatusCode.INVALID_ARGUMENT.value[0],
            message="invalid payload",
        )
        status.details.add().Pack(detail)
        context.set_trailing_metadata(
            (("grpc-status-details-bin", status.SerializeToString()),)
        )
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid payload")

    def slow(request: Any, context: grpc.ServicerContext) -> Any:
        while context.is_active():
            time.sleep(0.005)
        return request

    deserialize = payload.FromString
    serialize = payload.SerializeToString
    return {
        "Unary": grpc.unary_unary_rpc_method_handler(
            unary,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "UnaryTextOnly": grpc.unary_unary_rpc_method_handler(
            unary_text_only,
            request_deserializer=text_only.FromString,
            response_serializer=serialize,
        ),
        "ServerStream": grpc.unary_stream_rpc_method_handler(
            server_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "ClientStream": grpc.stream_unary_rpc_method_handler(
            client_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "ClientStreamTextOnly": grpc.stream_unary_rpc_method_handler(
            client_stream_text_only,
            request_deserializer=text_only.FromString,
            response_serializer=serialize,
        ),
        "Bidi": grpc.stream_stream_rpc_method_handler(
            bidi,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "TerminalClientStream": grpc.stream_unary_rpc_method_handler(
            terminal_client_stream,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "RichError": grpc.unary_unary_rpc_method_handler(
            rich_error,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
        "Slow": grpc.unary_unary_rpc_method_handler(
            slow,
            request_deserializer=deserialize,
            response_serializer=serialize,
        ),
    }


def _stable_reflection_handler(
    schema: Schema,
    expected_hosts: list[str] | None = None,
    error_message: str | None = None,
) -> grpc.GenericRpcHandler:
    file_proto = descriptor_pb2.FileDescriptorProto.FromString(
        reflection_pb2.DESCRIPTOR.serialized_pb
    )
    file_proto.name = "grpc/reflection/v1/reflection.proto"
    file_proto.package = "grpc.reflection.v1"

    def update_message_types(message: descriptor_pb2.DescriptorProto) -> None:
        for field in message.field:
            if field.type_name.startswith(".grpc.reflection.v1alpha."):
                field.type_name = field.type_name.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )
        for nested in message.nested_type:
            update_message_types(nested)

    for message in file_proto.message_type:
        update_message_types(message)
    for service in file_proto.service:
        for method in service.method:
            method.input_type = method.input_type.replace(
                ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
            )
            method.output_type = method.output_type.replace(
                ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
            )

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    request_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("grpc.reflection.v1.ServerReflectionRequest")
    )
    response_class = message_factory.GetMessageClass(
        pool.FindMessageTypeByName(
            "grpc.reflection.v1.ServerReflectionResponse"
        )
    )
    schema_files = descriptor_pb2.FileDescriptorSet.FromString(
        schema.protoset.read_bytes()
    ).file

    def reflect(
        requests: Iterator[Any], context: grpc.ServicerContext
    ) -> Iterator[Any]:
        for request in requests:
            if expected_hosts is not None and request.host != expected_hosts[0]:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"unexpected reflection host: {request.host!r}",
                )
            response = response_class(valid_host=request.host)
            response.original_request.CopyFrom(request)
            if error_message is not None:
                response.error_response.error_code = (
                    grpc.StatusCode.INTERNAL.value[0]
                )
                response.error_response.error_message = error_message
                yield response
                continue
            request_kind = request.WhichOneof("message_request")
            if request_kind == "list_services":
                response.list_services_response.service.add(name=SERVICE_NAME)
                response.list_services_response.service.add(
                    name="grpc.reflection.v1.ServerReflection"
                )
            elif request_kind in {"file_by_filename", "file_containing_symbol"}:
                response.file_descriptor_response.file_descriptor_proto.extend(
                    file.SerializeToString() for file in schema_files
                )
            elif request_kind == "all_extension_numbers_of_type":
                response.all_extension_numbers_response.base_type_name = (
                    request.all_extension_numbers_of_type
                )
            else:
                response.error_response.error_code = (
                    grpc.StatusCode.NOT_FOUND.value[0]
                )
                response.error_response.error_message = "descriptor not found"
            yield response

    return grpc.method_handlers_generic_handler(
        "grpc.reflection.v1.ServerReflection",
        {
            "ServerReflectionInfo": grpc.stream_stream_rpc_method_handler(
                reflect,
                request_deserializer=request_class.FromString,
                response_serializer=response_class.SerializeToString,
            )
        },
    )


def _make_server(
    schema: Schema,
    *,
    credentials: grpc.ServerCredentials | None = None,
    stable_reflection: bool = False,
    expected_reflection_hosts: list[str] | None = None,
    reflection_error: str | None = None,
) -> RunningServer:
    records: list[tuple[str, tuple[tuple[str, str | bytes], ...]]] = []
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=8),
        interceptors=(MetadataRecorder(records),),
    )
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(SERVICE_NAME, _handlers(schema)),)
    )
    if stable_reflection:
        server.add_generic_rpc_handlers(
            (
                _stable_reflection_handler(
                    schema, expected_reflection_hosts, reflection_error
                ),
            )
        )
    else:
        reflection.enable_server_reflection(
            (SERVICE_NAME, reflection.SERVICE_NAME), server, pool=schema.pool
        )
    address = "localhost:0"
    port = (
        server.add_secure_port(address, credentials)
        if credentials is not None
        else server.add_insecure_port(address)
    )
    assert port
    server.start()
    return RunningServer(f"localhost:{port}", records, server)
