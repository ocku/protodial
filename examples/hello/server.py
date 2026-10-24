"""Unary gRPC example with reflection and response metadata."""

import logging
import time
from concurrent import futures

import grpc
import hello_pb2
import hello_pb2_grpc
from grpc_reflection.v1alpha import reflection
from server_logging import configure_logging, elapsed_ms, log_event

PORT = 50051
EXAMPLE = "hello"
SERVICE = hello_pb2.DESCRIPTOR.services_by_name["Greeter"].full_name
LOGGER = configure_logging(EXAMPLE)


class Greeter(hello_pb2_grpc.GreeterServicer):
    """Return a greeting and echo a request metadata value."""

    def SayHello(self, request, context):
        """Return one greeting response."""
        started_at = time.monotonic()
        metadata = dict(context.invocation_metadata())
        request_id = metadata.get("x-request-id", "not-supplied")
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=f"{SERVICE}/SayHello",
            name=request.name,
            peer=context.peer(),
            request_id=request_id,
        )
        context.send_initial_metadata((("example-server", "hello"),))
        context.set_trailing_metadata((("request-id", request_id),))
        response = hello_pb2.HelloReply(
            message=f"Hello, {request.name}!",
            request_id=request_id,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=f"{SERVICE}/SayHello",
            responses=1,
        )
        return response


def serve() -> None:
    """Start the example server and wait until it is terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    hello_pb2_grpc.add_GreeterServicer_to_server(Greeter(), server)
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
