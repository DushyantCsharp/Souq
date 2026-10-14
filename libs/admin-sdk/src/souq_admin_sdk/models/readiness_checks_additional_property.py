from enum import StrEnum


class ReadinessChecksAdditionalProperty(StrEnum):
    FAILING = "failing"
    OK = "ok"

    def __str__(self) -> str:
        return str(self.value)
