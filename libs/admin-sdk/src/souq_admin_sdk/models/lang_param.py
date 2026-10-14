from enum import StrEnum


class LangParam(StrEnum):
    AR = "ar"
    AUTO = "auto"
    EN = "en"

    def __str__(self) -> str:
        return str(self.value)
