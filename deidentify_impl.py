"""Deterministic removal of direct patient identifiers.

The function returns:
    - redacted text for parser/LLM;
    - non-sensitive audit metadata.

Original identifiers are never returned in metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class DeidentificationAudit:
    patient_name_redacted: bool = False
    birth_date_redacted: bool = False
    age_redacted: bool = False
    medical_record_number_redacted: bool = False
    medical_card_number_redacted: bool = False
    department_number_redacted: bool = False
    identifier_occurrences: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _replace_with_marker(
    text: str,
    pattern: str,
    marker: str,
    flags: int = re.IGNORECASE,
) -> tuple[str, int]:
    compiled = re.compile(pattern, flags)
    text, count = compiled.subn(marker, text)
    return text, count


def deidentify_text(text: str) -> tuple[str, dict[str, object]]:
    """Remove direct patient identifiers from an epicrisis.

    The original values are not returned or stored.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    audit = DeidentificationAudit()
    total = 0

    # 1. Patient name.
    patient_name_pattern = (
        r"(\bПациент(?:ка)?\s*(?::\s*|—\s*|\s+))"
        r"([А-ЯЁ][а-яё-]+"
        r"(?:\s+[А-ЯЁ][а-яё-]+){1,3})"
    )
    text, count = _replace_with_marker(
        text,
        patient_name_pattern,
        r"\1[REDACTED_PATIENT]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "patient_name_redacted": True,
            }
        )
        total += count

    # 2. Date of birth.
    birth_date_pattern = (
        r"(\b(?:д\.?\s*р\.?|дата\s+рождения)\s*[:=]?\s*)"
        r"\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    )
    text, count = _replace_with_marker(
        text,
        birth_date_pattern,
        r"\1[REDACTED_BIRTH_DATE]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "birth_date_redacted": True,
            }
        )
        total += count

    # 3. Age (explicit "возраст X лет").
    age_pattern = (
        r"(\bвозраст\s*[:=]?\s*)"
        r"\d{1,3}\s*(?:лет|года|год)\b"
    )
    text, count = _replace_with_marker(
        text,
        age_pattern,
        r"\1[REDACTED_AGE]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "age_redacted": True,
            }
        )
        total += count

    # 4. Age in header line (e.g. "возраст 201 год").
    header_age_pattern = (
        r"(?m)^([^\n]*?\bПациент(?:ка)?\b[^\n]*?)"
        r"(\b\d{1,3}\s*(?:лет|года|год)\b)"
    )
    text, count = _replace_with_marker(
        text,
        header_age_pattern,
        r"\1[REDACTED_AGE]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "age_redacted": True,
            }
        )
        total += count

    # 5. Medical record number.
    record_number_pattern = (
        r"(\b(?:история\s+болезни|история\s+стационарного\s+больного)"
        r"\s*(?:№|N|номер)?\s*)\d{1,30}"
    )
    text, count = _replace_with_marker(
        text,
        record_number_pattern,
        r"\1[REDACTED_MEDICAL_RECORD_NUMBER]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "medical_record_number_redacted": True,
            }
        )
        total += count

    # 6. Medical card number.
    card_number_pattern = (
        r"(\b(?:медицинск(?:ая|ой)\s+карт(?:а|ы)|медкарт(?:а|ы))"
        r"\s*(?:№|N|номер)?\s*)\d{1,30}"
    )
    text, count = _replace_with_marker(
        text,
        card_number_pattern,
        r"\1[REDACTED_MEDICAL_CARD_NUMBER]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "medical_card_number_redacted": True,
            }
        )
        total += count

    # 7. Department number.
    department_pattern = (
        r"(\b(?:кардиологическ(?:ое|ого|ом)\s+отделени(?:е|я|и)|"
        r"отделени(?:е|я|и))"
        r"\s*(?:№|N|номер)?\s*)\d{1,10}"
    )
    text, count = _replace_with_marker(
        text,
        department_pattern,
        r"\1[REDACTED_DEPARTMENT_NUMBER]",
    )
    if count:
        audit = DeidentificationAudit(
            **{
                **audit.to_dict(),
                "department_number_redacted": True,
            }
        )
        total += count

    audit = DeidentificationAudit(
        **{
            **audit.to_dict(),
            "identifier_occurrences": total,
        }
    )

    result = audit.to_dict()
    result["deidentified"] = True
    result["raw_identifiers_retained"] = False

    return text, result
