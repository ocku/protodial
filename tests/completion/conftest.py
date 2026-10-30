import pytest

import protodial.completion._saved_protos as saved_protos_module


@pytest.fixture
def schema_compiler_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test schema results independently of compiler startup on busy runners.

    Timeout tests use the production budget and do not request this fixture.
    """
    monkeypatch.setattr(saved_protos_module, "_COMPILE_SECONDS", 5.0)
