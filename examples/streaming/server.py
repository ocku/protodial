"""Streaming gRPC example covering every streaming cardinality."""

import logging
import time
from collections.abc import Iterator
from concurrent import futures

import grpc
import streaming_pb2
import streaming_pb2_grpc
from grpc_reflection.v1alpha import reflection
from server_logging import configure_logging, elapsed_ms, log_event

PORT = 50052
EXAMPLE = "streaming"
SERVICE = streaming_pb2.DESCRIPTOR.services_by_name["Streamer"].full_name
LOGGER = configure_logging(EXAMPLE)


class Streamer(streaming_pb2_grpc.StreamerServicer):
    """Implement server, client, and bidirectional streams."""

    def Expand(self, request, context) -> Iterator[streaming_pb2.Item]:
        """Emit the requested text a fixed number of times."""
        started_at = time.monotonic()
        method = f"{SERVICE}/Expand"
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=method,
            peer=context.peer(),
            requested_responses=request.count,
            text=request.text,
        )
        for sequence in range(1, request.count + 1):
            log_event(
                LOGGER,
                logging.DEBUG,
                "rpc.message_sent",
                method=method,
                sequence=sequence,
            )
            yield streaming_pb2.Item(
                text=request.text,
                sequence=sequence,
            )
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=method,
            requests=1,
            responses=request.count,
        )

    def Collect(self, request_iterator, context) -> streaming_pb2.Collection:
        """Collect all client-stream values into one response."""
        started_at = time.monotonic()
        method = f"{SERVICE}/Collect"
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=method,
            peer=context.peer(),
        )
        values = []
        for sequence, item in enumerate(request_iterator, start=1):
            values.append(item.text)
            log_event(
                LOGGER,
                logging.DEBUG,
                "rpc.message_received",
                method=method,
                sequence=sequence,
                text=item.text,
            )
        response = streaming_pb2.Collection(
            values=values,
            count=len(values),
        )
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=method,
            requests=len(values),
            responses=1,
        )
        return response

    def Chat(self, request_iterator, context) -> Iterator[streaming_pb2.Item]:
        """Uppercase each bidirectional-stream value as it arrives."""
        started_at = time.monotonic()
        method = f"{SERVICE}/Chat"
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=method,
            peer=context.peer(),
        )
        message_count = 0
        for sequence, item in enumerate(request_iterator, start=1):
            message_count = sequence
            log_event(
                LOGGER,
                logging.DEBUG,
                "rpc.message_received",
                method=method,
                sequence=sequence,
                text=item.text,
            )
            yield streaming_pb2.Item(
                text=item.text.upper(),
                sequence=sequence,
            )
            log_event(
                LOGGER,
                logging.DEBUG,
                "rpc.message_sent",
                method=method,
                sequence=sequence,
            )
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=method,
            requests=message_count,
            responses=message_count,
        )


def serve() -> None:
    """Start the example server and wait until it is terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    streaming_pb2_grpc.add_StreamerServicer_to_server(Streamer(), server)
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
