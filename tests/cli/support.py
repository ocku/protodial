from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_proto_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class FakeRpcError(Exception):
    def code(self) -> object:
        return SimpleNamespace(name="INTERNAL")

    def details(self) -> str:
        return str(self)
