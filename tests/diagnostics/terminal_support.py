from __future__ import annotations

import io
from collections.abc import Sequence
from typing import overload


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class _CountingProperties(Sequence[int]):
    def __init__(self, values: Sequence[int]) -> None:
        self._values = tuple(values)
        self.reads = 0

    @overload
    def __getitem__(self, index: int) -> int: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[int, ...]: ...

    def __getitem__(self, index: int | slice) -> int | tuple[int, ...]:
        if isinstance(index, slice):
            values = self._values[index]
            self.reads += len(values)
            return values
        self.reads += 1
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)
