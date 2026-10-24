"""Structured gRPC error example with google.rpc.Status details."""

import logging
import time
from concurrent import futures

import errors_pb2
import errors_pb2_grpc
import grpc
from google.protobuf import any_pb2
from google.rpc import error_details_pb2, status_pb2
from grpc_reflection.v1alpha import reflection
from server_logging import configure_logging, elapsed_ms, log_event

PORT = 50053
EXAMPLE = "errors"
SERVICE = errors_pb2.DESCRIPTOR.services_by_name["Validator"].full_name
LOGGER = configure_logging(EXAMPLE)


class Validator(errors_pb2_grpc.ValidatorServicer):
    """Validate resources and return rich errors for invalid input."""

    def Validate(self, request, context):
        """Accept nonempty resources or abort with field violations."""
        started_at = time.monotonic()
        log_event(
            LOGGER,
            logging.INFO,
            "rpc.started",
            method=f"{SERVICE}/Validate",
            peer=context.peer(),
            resource=request.resource,
        )
        if request.resource:
            response = errors_pb2.ValidateReply(
                resource=request.resource,
                valid=True,
            )
            log_event(
                LOGGER,
                logging.INFO,
                "rpc.completed",
                code="OK",
                elapsed_ms=elapsed_ms(started_at),
                method=f"{SERVICE}/Validate",
                responses=1,
            )
            return response

        bad_request = error_details_pb2.BadRequest()
        violation = bad_request.field_violations.add()
        violation.field = "resource"
        violation.description = "resource must not be empty"
        packed = any_pb2.Any()
        packed.Pack(bad_request)
        status = status_pb2.Status(
            code=grpc.StatusCode.INVALID_ARGUMENT.value[0],
            message="validation failed",
            details=[packed],
        )
        context.set_trailing_metadata(
            (("grpc-status-details-bin", status.SerializeToString()),)
        )
        log_event(
            LOGGER,
            logging.WARNING,
            "rpc.rejected",
            code="INVALID_ARGUMENT",
            elapsed_ms=elapsed_ms(started_at),
            field="resource",
            method=f"{SERVICE}/Validate",
            reason="resource must not be empty",
        )
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "validation failed")


def serve() -> None:
    """Start the example server and wait until it is terminated."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    errors_pb2_grpc.add_ValidatorServicer_to_server(Validator(), server)
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
