from types import SimpleNamespace

import pytest

from protodial.rpc_core.execution import Execution, run_execution


@pytest.mark.parametrize(
    "primary", [BrokenPipeError("closed"), KeyboardInterrupt()]
)
def test_rpc_cleanup_preserves_primary_failure(primary: BaseException) -> None:
    cleanup = OSError("cancel failed")

    def cancel() -> None:
        raise cleanup

    def action() -> None:
        raise primary

    execution = Execution(call=SimpleNamespace(cancel=cancel))
    with pytest.raises(type(primary)) as caught:
        run_execution(action, None, execution, RuntimeError)

    assert caught.value is primary
    assert any(
        "RPC cancellation also failed" in note and "cancel failed" in note
        for note in getattr(primary, "_protodial_notes", ())
    )


def test_rpc_cleanup_failure_is_visible_when_action_did_not_fail() -> None:
    def cancel() -> None:
        raise OSError("cancel failed")

    execution = Execution(call=SimpleNamespace(cancel=cancel))
    with pytest.raises(OSError, match="cancel failed"):
        run_execution(lambda: None, None, execution, RuntimeError)
