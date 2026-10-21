from __future__ import annotations

import re
import unicodedata

DEFAULT_CLASSIFICATION_QUEUE_PRIORITY = 0
ELEVATED_CLASSIFICATION_QUEUE_PRIORITY = 100

_PAYMENT_TERMS = r"(?:cobrancas?|compras?|transacoes?|pagamentos?)"
_UNRECOGNIZED_OR_DUPLICATE = (
    r"(?:nao reconhec(?:o|e|emos|i|ido|ida|idos|idas)|desconhecid[ao]s?|duplicad[ao]s?)"
)
_STRONG_URGENCY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bfraud(?:e|es|ulenta|ulentas|ulento|ulentos)\b",
        r"\binvasao\b.{0,40}\bconta\b",
        r"\bconta\b.{0,50}\b(?:invadid[ao]s?|hackead[ao]s?|comprometid[ao]s?)\b",
        r"\b(?:invadiram|hackearam|comprometeram)\b.{0,50}\bconta\b",
        r"\bcart(?:ao|oes)\b.{0,40}\bclonad[ao]s?\b",
        rf"\b{_PAYMENT_TERMS}\b.{{0,50}}\b{_UNRECOGNIZED_OR_DUPLICATE}\b",
        rf"\b{_UNRECOGNIZED_OR_DUPLICATE}\b.{{0,50}}\b{_PAYMENT_TERMS}\b",
        r"\bseguranca\b.{0,50}\bcomprometid[ao]s?\b",
        r"\bcomprometimento\b.{0,50}\bseguranca\b",
        r"\b(?:urgente(?:mente)?|urgencia|emergencia)\b",
    )
)


def _normalize_for_scheduling(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(without_accents.split())


def classification_queue_priority(*, subject: str, message: str) -> int:
    """Prioritize strong urgency signals without classifying the ticket."""
    normalized_ticket = _normalize_for_scheduling(f"{subject} {message}")
    if any(pattern.search(normalized_ticket) for pattern in _STRONG_URGENCY_PATTERNS):
        return ELEVATED_CLASSIFICATION_QUEUE_PRIORITY
    return DEFAULT_CLASSIFICATION_QUEUE_PRIORITY
