from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool
from grpc_tools import protoc

from protodial.presentation.descriptor import render_descriptor


@pytest.mark.parametrize("client_streaming", [False, True])
@pytest.mark.parametrize("server_streaming", [False, True])
def test_rendered_service_method_options_compile_and_preserve_semantics(
    tmp_path: Path, client_streaming: bool, server_streaming: bool
) -> None:
    proto = descriptor_pb2.FileDescriptorProto(
        name="original.proto", syntax="proto3"
    )
    proto.message_type.add(name="Payload")
    service = proto.service.add(name="API")
    for name, deprecated, idempotency in (
        ("Legacy", True, descriptor_pb2.MethodOptions.NO_SIDE_EFFECTS),
        ("Current", False, descriptor_pb2.MethodOptions.IDEMPOTENT),
        ("Default", None, None),
    ):
        method = service.method.add(
            name=name,
            input_type=".Payload",
            output_type=".Payload",
        )
        if client_streaming:
            method.client_streaming = True
        if server_streaming:
            method.server_streaming = True
        if deprecated is not None:
            method.options.deprecated = deprecated
        if idempotency is not None:
            method.options.idempotency_level = idempotency
    pool = descriptor_pool.DescriptorPool()
    descriptor = pool.Add(proto).services_by_name["API"]
    rendered = render_descriptor(descriptor)
    for method in descriptor.methods:
        standalone = render_descriptor(method)
        assert standalone in rendered.replace("\n  ", "\n")
    source = tmp_path / "rendered.proto"
    source.write_text(
        'syntax = "proto3";\nmessage Payload {}\n' + rendered + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "compiled.pb"

    assert (
        protoc.main(
            [
                "test-method-rendering",
                f"--proto_path={tmp_path}",
                f"--descriptor_set_out={output}",
                str(source),
            ]
        )
        == 0
    )

    result = descriptor_pb2.FileDescriptorSet.FromString(output.read_bytes())
    compiled_service = result.file[0].service[0]
    assert compiled_service == service
