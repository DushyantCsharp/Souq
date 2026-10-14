from enum import StrEnum


class InvalidParamLocation(StrEnum):
    BODY = "body"
    HEADER = "header"
    PATH = "path"
    QUERY = "query"

    def __str__(self) -> str:
        return str(self.value)
