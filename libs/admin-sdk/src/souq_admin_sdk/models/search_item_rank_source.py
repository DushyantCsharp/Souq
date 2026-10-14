from enum import StrEnum


class SearchItemRankSource(StrEnum):
    FUSED = "fused"
    LEXICAL_TAIL = "lexical_tail"
    RERANK = "rerank"

    def __str__(self) -> str:
        return str(self.value)
