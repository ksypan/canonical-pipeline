"""Explicit case rules and an intentionally narrow ICD-10 codebook.

No ECG or coronary anatomy is used to infer MI localisation.
"""
import re
from decimal import Decimal

ICD_LOCALISATION_SOURCE = "https://classbrowser.nhs.uk/ICD-10-5TH-Edition/vol1/block-i20-i25.htm"
ICD_MI_LOCALISATION = {
    "I21.0": "передней стенки",
    "I21.1": "нижней стенки",
}
ICD_LOCALISATION_CLARIFICATION = {
    "I21.2": "Other sites covers several current localisation categories.",
    "I21.3": "Site unspecified.",
    "I21.4": "Subendocardial infarction does not specify a wall.",
    "I21.9": "Infarction unspecified.",
}
CORONARY_VESSELS = {"ca_lad": ["LAD", "ПМЖВ"], "rca": ["RCA", "ПКА"]}
NO_SIGNIFICANT_STENOSIS = r"гемодинамически\s+значимого\s+стеноза\s+нет"


def stenosis_category(percent):
    """Do not round or bridge the unresolved 89–90% interval."""
    percent = Decimal(str(percent).replace(",", "."))
    if 0 <= percent < 50:
        return 0
    if 50 <= percent <= 89:
        return 1
    if 90 <= percent <= 100:
        return 2
    return None


def coronary_findings(text):
    """Vessel-scoped, explicit observations; disagreements stay unresolved.

    Returns field -> (category or None, short verbatim fragment or None).
    A None category flags an ambiguous observation, rather than its absence.
    """
    aliases = {alias.casefold(): field for field, names in CORONARY_VESSELS.items() for alias in names}
    matches = list(re.finditer(r"\b(?:LAD|ПМЖВ|RCA|ПКА)\b", text, re.IGNORECASE))
    observations = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # A vessel label must not borrow the next line/semicolon's finding.
        stops = [p for p in (text.find("\n", match.end()), text.find(";", match.end())) if p >= 0]
        end = min([end, *stops])
        fragment = text[match.start():end].strip()
        field = aliases[match.group().casefold()]
        uncertain = re.search(r"\?|не\s+исключ|подозр|возмож|вероят|ранее|в\s+анамнезе|рекоменд|планир|\bесли\b", fragment, re.IGNORECASE)
        categories = []
        if re.search(NO_SIGNIFICANT_STENOSIS, fragment, re.IGNORECASE):
            categories.append(0)
        percentages = re.findall(r"(?<![\d.,])\d+(?:[.,]\d+)?\s*%", fragment)
        # Different lesions/ranges lack a project selection rule.
        if percentages and (len(set(percentages)) > 1 or re.search(r"\d\s*[-–—]\s*\d|[<>≤≥]", fragment)):
            categories.append(None)
        else:
            categories.extend(stenosis_category(p.rstrip("% ")) for p in percentages)
        for occlusion in re.finditer(r"\bокклюзи\w*", fragment, re.IGNORECASE):
            before, after = fragment[:occlusion.start()], fragment[occlusion.end():]
            if not re.search(r"(?:без|нет|не)\s*$", before, re.IGNORECASE) and not re.match(r"\s+(?:нет|не|отсутств)", after, re.IGNORECASE):
                categories.append(2)
        if not categories:
            continue
        value = categories[0] if not uncertain and all(c == categories[0] and c is not None for c in categories) else None
        observations.setdefault(field, []).append((value, fragment if len(fragment) <= 240 else None))
    result = {}
    for field, items in observations.items():
        first = items[0][0]
        result[field] = (first, items[0][1]) if first is not None and all(value == first for value, _ in items) else (None, None)
    return result
