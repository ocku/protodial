"""Adapt packaged reflection descriptors to the stable v1 wire schema."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import cast

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .contracts import ReflectionWireModule


def stable_wire_module(wire_v1alpha: ModuleType) -> ReflectionWireModule:
    """Load stable reflection classes or build them from the alpha schema."""
    try:
        module = import_module("grpc_reflection.v1.reflection_pb2")
        return cast(ReflectionWireModule, cast(object, module))
    except ImportError:
        # Some grpcio-reflection releases package only v1alpha. Build v1 classes
        # from that wire-identical schema rather than using v1alpha classes on
        # the stable endpoint.
        v1_file = descriptor_pb2.FileDescriptorProto.FromString(
            wire_v1alpha.DESCRIPTOR.serialized_pb
        )
        v1_file.name = "grpc/reflection/v1/reflection.proto"
        v1_file.package = "grpc.reflection.v1"

        def update_message_types(
            message: descriptor_pb2.DescriptorProto,
        ) -> None:
            for field in message.field:
                if field.type_name.startswith(".grpc.reflection.v1alpha."):
                    field.type_name = field.type_name.replace(
                        ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                    )
            for nested in message.nested_type:
                update_message_types(nested)

        for message in v1_file.message_type:
            update_message_types(message)
        for service in v1_file.service:
            for method in service.method:
                method.input_type = method.input_type.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )
                method.output_type = method.output_type.replace(
                    ".grpc.reflection.v1alpha.", ".grpc.reflection.v1."
                )

        v1_pool = descriptor_pool.DescriptorPool()
        v1_pool.Add(v1_file)
        module = SimpleNamespace(
            ServerReflectionRequest=message_factory.GetMessageClass(
                v1_pool.FindMessageTypeByName(
                    "grpc.reflection.v1.ServerReflectionRequest"
                )
            ),
            ServerReflectionResponse=message_factory.GetMessageClass(
                v1_pool.FindMessageTypeByName(
                    "grpc.reflection.v1.ServerReflectionResponse"
                )
            ),
        )
        return cast(ReflectionWireModule, cast(object, module))
