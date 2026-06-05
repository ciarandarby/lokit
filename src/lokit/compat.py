from enum import Enum
from typing import cast


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return cast("str", self.value)
