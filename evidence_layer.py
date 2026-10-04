"""Service-only quotations and explicit clinical consistency checks.

The official 50-field object never contains this metadata. A verbatim match
is necessary but not sufficient: LLM quotations also need conservative
value/scope checks. Unsupported quotations are omitted, never invented.
"""
from copy import deepcopy
import re

from clinical_rules import ICD_MI_LOCALISATION, ICD_LOCALISATION_CLARIFICATION, coronary_findings
from field_contract import FIELD_CONTRACT, NOT_SPECIFIED
from parser import split_sections, initial_section, is_asserted, is_certain, evidence, create_empty_template, extract_xray
from pipeline_errors import FinalizationError

MAX_EVIDENCE_LENGTH = 240


def explicit_mi_localisation(diagnosis):
    """Return (value, quotation, localisation_mentioned); never use ECG/CA."""
    found = []
    mentioned = False
    for match in re.finditer(r"\b(?:инфаркт\w*\s+миокарда|ИМ)\b[^\n;.!?]{0,180}", diagnosis, re.IGNORECASE):
        fragment = match.group()
        head = re.compile(r"\b(?:инфаркт\w*\s+миокарда|ИМ)\b", re.IGNORECASE).match(diagnosis, match.start())
        asserted = is_asserted(diagnosis, head) and not re.search(
            r"не\s+(?:подтвержд\w*|выявлен\w*|установлен\w*)|исключ[её]н|отрицается", fragment, re.IGNORECASE)
        locations = []
        for category in sorted(FIELD_CONTRACT["mi_localisation"]["allowed_texts"], key=len, reverse=True):
            if category.casefold() in fragment.casefold() and not any(category in longer for longer in locations):
                locations.append(category)
        if re.search(r"стенк|перегородк|прав\w*\s+желудоч", fragment, re.IGNORECASE):
            mentioned = True
        if locations and asserted:
            if len(locations) == 1:
                found.append(({"code": 1, "text": locations[0]}, evidence(diagnosis, match)))
            else:
                return None, None, True
        elif not asserted:
            # A negated/uncertain MI is not a licence for ICD fallback.
            return None, None, True
    if found and all(value == found[0][0] for value, _ in found):
        return found[0][0], found[0][1], True
    return None, None, mentioned or bool(found)


def apply_clinical_consistency(values, record, source_text):
    """Resolve text-first MI/ICD and explicit vessel/X-ray facts, without mutation."""
    result = deepcopy(values)
    overrides, sources, clarifications = {}, {}, []
    sections = split_sections(source_text)
    diagnosis = "\n".join(section.text for section in sections if section.kind == "diagnosis")
    explicit, quote, mentioned = explicit_mi_localisation(diagnosis)
    localisation = explicit
    provenance = "diagnosis_text"
    if explicit is None and not mentioned:
        code = result.get("diagnosis_icd")
        meta = record.get("meta", {}).get("diagnosis_icd", {})
        code_quote = meta.get("evidence")
        # The source ICD must be the unchanged confirmed diagnosis value.
        valid_code = (meta.get("status") == "confirmed" and meta.get("source") == "parser"
                      and record["values"].get("diagnosis_icd") == code
                      and isinstance(code, str) and isinstance(code_quote, str)
                      and code_quote in diagnosis and re.search(r"(?<!\w)" + re.escape(code) + r"(?!\w|\.\d)", code_quote))
        if valid_code and code in ICD_MI_LOCALISATION:
            localisation = {"code": 1, "text": ICD_MI_LOCALISATION[code]}
            quote, provenance = code_quote, "icd_fallback"
        elif valid_code and code.startswith("I21"):
            clarifications.append("CONTRACT_CLARIFICATION: mi_localisation " + code + ": "
                                  + ICD_LOCALISATION_CLARIFICATION.get(code, "Code outside the approved exact mapping."))
    if localisation is None:
        localisation = deepcopy(FIELD_CONTRACT["mi_localisation"]["missing_value"])
        if mentioned:
            clarifications.append("CONTRACT_CLARIFICATION: explicit MI localisation is ambiguous or unsupported.")

    def assign(field, value, fragment, origin):
        entry = record.get("meta", {}).get(field, {})
        if entry.get("status") == "confirmed" and record["values"].get(field) != value:
            raise FinalizationError("Source consistency conflicts with a confirmed parser field: " + field)
        result[field] = deepcopy(value)
        if isinstance(fragment, str) and 0 < len(fragment) <= MAX_EVIDENCE_LENGTH and fragment in source_text:
            overrides[field], sources[field] = fragment, origin

    # Existing confirmed parser fields are never replaced by LLM evidence.
    if record.get("meta", {}).get("mi_localisation", {}).get("status") != "confirmed":
        assign("mi_localisation", localisation, quote if localisation["code"] == 1 else None, provenance)
    if result.get("ca_fact") == "Y":
        for field, (category, fragment) in coronary_findings(initial_section(sections, "ca")).items():
            value = FIELD_CONTRACT[field]["missing_value"] if category is None else category
            assign(field, value, fragment, "coronary_text")
            if category is None:
                clarifications.append("CONTRACT_CLARIFICATION: ambiguous coronary observation for " + field)
    xray = create_empty_template()
    extract_xray(xray, initial_section(sections, "xray"))
    if xray["meta"]["rg_pc"]["status"] == "confirmed":
        assign("rg_pc", xray["values"]["rg_pc"], xray["meta"]["rg_pc"]["evidence"], "xray_text")
    return result, overrides, sources, clarifications


def _in_scope(field, quote, source_text):
    scope = FIELD_CONTRACT[field].get("section")
    allowed = {scope} if isinstance(scope, str) else set(scope or [])
    if not allowed:
        return True
    for section in split_sections(source_text):
        if section.kind in allowed and quote in section.text:
            return True
        if "treatment" in allowed and section.kind == "other" and quote in section.text:
            if re.match(r"\s*(?:#+\s*)?(?:\*\*)?(?:Провед[её]нное\s+лечение|Терапия|Лечение)", section.text, re.IGNORECASE):
                return True
    return False


def _supports(field, value, quote):
    descriptor = FIELD_CONTRACT[field]
    kind = descriptor["value_type"]
    lower = quote.casefold()
    if not is_certain(quote, re.match(r"[\s\S]+", quote)):
        return False
    if field in {"ca_lad", "rca"}:
        found = coronary_findings(quote).get(field)
        return found is not None and found[0] is not None and found[0] == value
    if field == "mi_localisation":
        explicit, _, _ = explicit_mi_localisation(quote)
        return explicit == value and explicit is not None
    if kind == "medication_list_or_zero":
        return isinstance(value, list) and all(
            item["name"].casefold() in lower and
            (item["prescription"] == NOT_SPECIFIED or item["prescription"].casefold() in lower)
            for item in value)
    if kind == "binary_code":
        if value["code"] == 1:
            return value["text"].casefold() in lower and not re.search(r"\b(?:нет|без|не|отрица\w*)\b|не\s+выяв", lower)
        if field == "smoking":
            return bool(re.search(r"\b(?:не\s+курит|бросил(?:а)?\s+курить|прекратил(?:а)?\s+курить)\b", lower))
        if field == "rg_pc":
            temporary = create_empty_template()
            extract_xray(temporary, quote)
            return temporary["values"][field] == value
        terms = [term for rule in descriptor.get("rules", []) for term in rule.get("explicit_terms", [])]
        return any(term.casefold() in lower for term in terms) and bool(re.search(r"\b(?:нет|без|не|отрица\w*)\b", lower))
    if value == descriptor["missing_value"]:
        return False
    if field == "ca_fact":
        if value == "Y":
            return bool(re.search(r"(?:выполнен\w*|проведен\w*|провед[её]н\w*)\s+(?:селективная\s+)?(?:КАГ|коронарографи\w*)|(?:КАГ|коронарография)\s+выполнен\w*", quote, re.IGNORECASE))
        return value == "R" and bool(re.search(r"отказ\w*.*(?:КАГ|коронарографи)", quote, re.IGNORECASE))
    if kind == "date":
        return value in quote.replace("/", ".")
    if kind in {"number", "integer"}:
        labels = [label for rule in descriptor.get("rules", []) for label in rule.get("labels", [])]
        return (any(label.casefold() in lower for label in labels)
                and bool(re.search(r"(?<![\d.,])" + re.escape(str(value)).replace(r"\.", r"[.,]") + r"(?![\d.,])", quote)))
    if not isinstance(value, str) or value.casefold() not in lower:
        return False
    # A negated occurrence of a category is not evidence for that category.
    return not re.search(r"\b(?:не|нет|без)\s+" + re.escape(value.casefold()), lower)


def validate_llm_evidence(values, proposed, source_text):
    accepted, rejected = {}, []
    for field, quote in proposed.items():
        if quote is None:
            continue
        if (field not in values or not isinstance(quote, str) or not 0 < len(quote) <= MAX_EVIDENCE_LENGTH
                or not quote.strip() or quote not in source_text or not _in_scope(field, quote, source_text)
                or not _supports(field, values[field], quote)):
            rejected.append(field)
        else:
            accepted[field] = quote
    return accepted, rejected


def collect_service_evidence(record, final, llm_values, llm_evidence, overrides=None, override_sources=None, *, source_text):
    """Only attach quotes for the values that survived defaults/dependencies."""
    result, sources = {}, {}
    for field, quote in llm_evidence.items():
        if final.get(field) == llm_values.get(field):
            result[field], sources[field] = quote, "llm"
    for field, entry in record["meta"].items():
        if (entry.get("status") == "confirmed" and final[field] == record["values"][field]
                and isinstance(entry.get("evidence"), str) and entry["evidence"] in source_text
                and 0 < len(entry["evidence"]) <= MAX_EVIDENCE_LENGTH):
            result[field], sources[field] = entry["evidence"], "parser"
    for field, quote in (overrides or {}).items():
        # A default caused by a failed dependency must not retain old evidence.
        if final[field] != FIELD_CONTRACT[field]["missing_value"] or field == "rg_pc":
            result[field], sources[field] = quote, (override_sources or {}).get(field, "source_rule")
    return result, sources
