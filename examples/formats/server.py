"""Message-format example with representative protobuf field shapes."""

import logging
import time
from concurrent import futures

import formats_pb2
import formats_pb2_grpc
import grpc
from grpc_reflection.v1alpha import reflection
from server_logging import configure_logging, elapsed_ms, log_event

PORT = 50054
EXAMPLE = "formats"
SERVICE = formats_pb2.DESCRIPTOR.services_by_name["Codec"].full_name
LOGGER = configure_logging(EXAMPLE)


class Codec(formats_pb2_grpc.CodecServicer):
    """Echo structured documents and raw byte payloads."""

    def EchoDocument(self, request, context):
        """Return a document containing common protobuf field shapes."""
        started_at = time.monotonic()
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=f"{SERVICE}/EchoDocument",
            peer=context.peer(),
            tags=len(request.tags),
        )
        response = formats_pb2.Document()
        response.CopyFrom(request)
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=f"{SERVICE}/EchoDocument",
            responses=1,
        )
        return response

    def EchoBlob(self, request, context):
        """Return an unchanged payload for raw input and output examples."""
        started_at = time.monotonic()
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            bytes=len(request.content),
            method=f"{SERVICE}/EchoBlob",
            peer=context.peer(),
        )
        response = formats_pb2.Blob(content=request.content)
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=f"{SERVICE}/EchoBlob",
            responses=1,
        )
        return response


def serve() -> None:
    """Start the example server and wait until it is terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    formats_pb2_grpc.add_CodecServicer_to_server(Codec(), server)
    reflection.enable_server_reflection(
        (SERVICE, reflection.SERVICE_NAME),
        server,
    )
    address = f"[::]:{PORT}"
    server.add_insecure_port(address)
    server.start()
    log_event(
        LOGGER,
        logging.INFO,
        "server.started",
        address=address,
        reflection=True,
        service=SERVICE,
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log_event(LOGGER, logging.INFO, "server.stopping", grace_seconds=5)
        server.stop(5).wait()
    finally:
        log_event(LOGGER, logging.INFO, "server.stopped")


if __name__ == "__main__":
    serve()
