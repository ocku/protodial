"""Deadline example that stops work when its RPC is cancelled."""

import logging
import threading
import time
from concurrent import futures

import deadlines_pb2
import deadlines_pb2_grpc
import grpc
from grpc_reflection.v1alpha import reflection
from server_logging import configure_logging, elapsed_ms, log_event

PORT = 50055
EXAMPLE = "deadlines"
SERVICE = deadlines_pb2.DESCRIPTOR.services_by_name["Sleeper"].full_name
LOGGER = configure_logging(EXAMPLE)


class Sleeper(deadlines_pb2_grpc.SleeperServicer):
    """Wait for a requested duration while observing cancellation."""

    def Sleep(self, request, context):
        """Return after a delay, or stop promptly when the deadline expires."""
        started_at = time.monotonic()
        method = f"{SERVICE}/Sleep"
        cancelled = threading.Event()
        context.add_callback(cancelled.set)
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            delay_ms=request.delay_ms,
            method=method,
            peer=context.peer(),
        )
        if cancelled.wait(request.delay_ms / 1000):
            log_event(
                LOGGER,
                logging.WARNING,
                "rpc.cancelled",
                code="CANCELLED",
                elapsed_ms=elapsed_ms(started_at),
                method=method,
            )
            return deadlines_pb2.SleepReply()

        log_event(
            LOGGER,
            logging.INFO,
            "rpc.completed",
            code="OK",
            elapsed_ms=elapsed_ms(started_at),
            method=method,
            responses=1,
        )
        return deadlines_pb2.SleepReply(slept_ms=request.delay_ms)


def serve() -> None:
    """Start the example server and wait until it is terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    deadlines_pb2_grpc.add_SleeperServicer_to_server(Sleeper(), server)
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
