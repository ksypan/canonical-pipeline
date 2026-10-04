"""Fail closed on obvious identifiers; never redact/rewrite an LLM request."""
import json
import re
from pipeline_errors import PrivacyError
from prompt_builder import assert_deidentified_text


LABELLED_IDENTIFIERS = (
    r"\b(?:Ф\s*\.\s*И\s*\.\s*О\s*\.?|ФИО|Фамилия|Имя\s+пациента|Пациент(?:ка)?|Patient(?:\s+name)?)\s*[:=]\s*[A-Za-zА-ЯЁа-яё]",
    r"\b(?:паспорт|СНИЛС|полис|телефон)\s*(?:№|номер|[:=])?\s*\+?\d[\d ()-]{5,}",
    r"\b(?:адрес|место\s+жительства)\s*[:=]\s*[^\[\s]",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
)


def assert_safe_request(request):
    """Inspect prompts and confirmed context, without exposing detected values."""
    if request.get("llm_required") is False:
        return
    text = "\n".join(str(request.get(key, "")) for key in ("system_prompt", "user_prompt"))
    if request.get("confirmed_values"):
        text += "\n" + json.dumps(request["confirmed_values"], ensure_ascii=False)
    try:
        assert_deidentified_text(text)
    except ValueError:
        raise PrivacyError("Unredacted patient identifiers detected; request/result blocked.") from None
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in LABELLED_IDENTIFIERS):
        raise PrivacyError("Unredacted patient identifiers detected; request/result blocked.")
