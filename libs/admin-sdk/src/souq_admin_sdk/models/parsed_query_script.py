from enum import StrEnum


class ParsedQueryScript(StrEnum):
    AR = "ar"
    ARABIZI = "arabizi"
    EN = "en"
    MIXED = "mixed"

    def __str__(self) -> str:
        return str(self.value)
