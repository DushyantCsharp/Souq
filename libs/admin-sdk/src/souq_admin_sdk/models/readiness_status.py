from enum import StrEnum


class ReadinessStatus(StrEnum):
    NOT_READY = "not_ready"
    READY = "ready"

    def __str__(self) -> str:
        return str(self.value)
