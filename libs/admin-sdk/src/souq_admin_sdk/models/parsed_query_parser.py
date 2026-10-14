from enum import StrEnum


class ParsedQueryParser(StrEnum):
    CACHE = "cache"
    LLM = "llm"
    RULE = "rule"

    def __str__(self) -> str:
        return str(self.value)
