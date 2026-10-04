"""Conservative extraction into an INTERNAL values/meta record, not submission JSON.

Only explicit text is accepted. Missing, conflicting and unsupported statements
remain None / needs_llm. No external dependencies, services or medical inference.

Processing order:
1. Remove direct patient identifiers.
2. Extract deterministic fields from the deidentified text.
3. Build an internal values/meta record.
4. Send only deidentified text to the LLM.

The final submission JSON contains only the official 50 fields.
"""


import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from deidentify import deidentify_text
from field_contract import FIELD_CONTRACT, NOT_SPECIFIED, DATE_STRPTIME


TEMPLATE_PATH = Path(__file__).with_name("participant-output-template-v2.json")
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("intermediate")
FLAGS = re.IGNORECASE
NUMBER = r"\d+(?:[,.]\d+)?"
DATE = r"\d{2}[./]\d{2}[./]\d{4}"
SEP = r"\s*(?:[:=—–-]\s*)?"
# Routing is derived from the contract.  Keeping these aliases makes the
# implementation readable without creating a second source of truth.
DEFERRED_FIELDS = frozenset(
    name for name, field in FIELD_CONTRACT.items() if field["extractor"] == "llm"
)
BINARY_CODE_FIELDS = frozenset(
    name for name, field in FIELD_CONTRACT.items() if field["value_type"] == "binary_code"
)


# These are structural aliases, not medical classifications. Subheadings of
# diagnosis (e.g. Основной диагноз) deliberately stay inside their parent.
SECTION_HEADINGS = {
    "diagnosis": r"(?:Заключительный\s+диагноз|Диагноз)",
    "exam": r"(?:Осмотр|Первичный\s+статус|Данные\s+осмотра|Первичный\s+осмотр)",
    "ecg": r"ЭКГ(?:\s+при\s+поступлении)?",
    "echo": r"(?:ЭхоКГ|Эхокардиография)",
    "labs": r"(?:Первичные\s+анализы(?:\s+крови)?|Анализы\s+крови)",
    "xray": r"(?:Рентгенография\s+(?:грудной\s+клетки|ОГК)|Рентгенограмма\s+ОГК|Рентген(?:\s+ОГК)?)",
    "ca": r"(?:КАГ|Коронарография)",
    "history": r"(?:Из\s+анамнеза|Анамнез(?:\s+(?:жизни|заболевания))?|Жалобы\s+при\s+поступлении)",
    "recommendations": r"(?:Рекомендации|Назначения\s+при\s+выписке)",
    "other": r"(?:Повторный\s+осмотр|Контрольный\s+осмотр|Состояние\s+в\s+динамике|Данные\s+наблюдения|Осмотры\s+специалистов|Консультативные\s+заключения|Провед[её]нное\s+лечение|Терапия\s+в\s+стационаре|Лечение\s+и\s+исход|Течение\s+госпитализации|План\s+наблюдения|Порядок\s+амбулаторного\s+контроля|Инструментальные\s+данные)",
}
HEADER_PREFIX = r"^[ \t]*(?:\#{1,6}\s*)?(?:\*\*)?"
HEADER_END = rf"(?=\s*(?:[:.\n]|\*\*|$)|\s+(?:от\s+)?{DATE})"
REPEAT = re.compile(
    r"\b(?:контрол[ьл]н[а-яё]*|контроль\b|повторн[а-яё]*)|перед\s+выпиской",
    FLAGS,
)
NONCURRENT = re.compile(
    r"\b(?:ранее|амбулаторн[а-яё]*|догоспитальн[а-яё]*|до\s+госпитализации|в\s+анамнезе|предыдущ[а-яё]*\s+госпитализац[а-яё]*)\b",
    FLAGS,
)


@dataclass(frozen=True)
class Section:
    kind: str
    text: str


def load_field_keys(template_path=TEMPLATE_PATH):
    """Use only official keys; ignore all submission defaults."""
    template = json.loads(Path(template_path).read_text(encoding="utf-8-sig"))
    if not isinstance(template, dict) or len(template) != 50:
        raise ValueError("Expected the official flat template containing 50 keys")
    return tuple(template)


def create_empty_template(template_path=TEMPLATE_PATH):
    keys = load_field_keys(template_path)
    return {
        "values": dict.fromkeys(keys),
        "meta": {key: {"status": "needs_llm", "source": None, "evidence": None}
                 for key in keys},
    }


def split_sections(text):
    """Find colon, standalone and inline-dot headings without whole-text fallback."""
    boundaries = {}
    for kind, alias in SECTION_HEADINGS.items():
        for match in re.finditer(HEADER_PREFIX + alias + HEADER_END, text, FLAGS | re.MULTILINE):
            boundaries[match.start()] = kind
    wrapper = r"^[ \t]*(?:\#{1,6}\s*)?(?:\*\*)?Инструментальные\s+данные\s*[.:]\s*"
    for match in re.finditer(wrapper + r"(?=ЭКГ\s+(?:от\s+)?" + DATE + r")", text, FLAGS | re.MULTILINE):
        boundaries[match.end()] = "ecg"
    # A plain content line must not become a heading merely because it starts
    # with Cyrillic text. Generic headings need explicit punctuation/markdown;
    # known headings are handled by SECTION_HEADINGS above.
    # A trailing sentence full stop is not an unknown section heading.
    # Known dot headings were already matched explicitly above.
    generic = r"^[ \t]*(?:\#{1,6}\s+|(?:\*\*)?[А-ЯЁа-яё][А-ЯЁа-яё /()–-]{2,75}(?:\*\*)?[ \t]*:)[ \t]*$"
    for match in re.finditer(generic, text, re.MULTILINE):
        if not re.match(r"\s*(?:Основной|Сопутствующ|Фонов|Осложнен)", match.group(), FLAGS):
            boundaries.setdefault(match.start(), "other")
    positions = sorted(boundaries)
    sections = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        section = Section(boundaries[start], text[start:end])
        if sections and sections[-1].kind == section.kind:
            alias = SECTION_HEADINGS.get(section.kind, r"(?!)")
            if re.fullmatch(r"\s*(?:\#{1,6}\s*)?(?:\*\*)?" + alias + r"(?:\*\*)?\s*[:.]?\s*", sections[-1].text, FLAGS):
                sections[-1] = Section(section.kind, sections[-1].text + section.text)
                continue
        sections.append(section)
    return sections


def extract_section(text, heading):
    """Compatibility helper: canonical name or regex matching a known heading."""
    for section in split_sections(text):
        if section.kind == heading or re.match(HEADER_PREFIX + heading + HEADER_END, section.text, FLAGS):
            return section.text
    return ""


def initial_section(sections, kind):
    """Use the first relevant record only, stopping before repeat measurements."""
    for section in sections:
        if section.kind == kind:
            match = REPEAT.search(section.text)
            return section.text[:match.start()] if match else section.text
    return ""


def to_number(value):
    if value is None:
        return None
    try:
        number = float(value.replace(",", "."))
        return int(number) if number.is_integer() else number
    except (ValueError, AttributeError):
        return None


def normalize_date(value):
    try:
        return datetime.strptime(value.replace("/", "."), DATE_STRPTIME).strftime(DATE_STRPTIME)
    except ValueError:
        return None


def evidence(text, match):
    """Short verbatim source span. Never reconstruct evidence from a value."""
    left = max(text.rfind("\n", 0, match.start()), text.rfind(";", 0, match.start())) + 1
    right_candidates = [p for p in (text.find("\n", match.end()), text.find(";", match.end())) if p >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    if right - left > 240:
        left, right = max(left, match.start() - 45), min(right, match.end() + 45)
    return text[left:right].strip()


def confirm(result, key, value, source_evidence):
    field = FIELD_CONTRACT.get(key)
    if (
        field is not None
        and field["extractor"] != "llm"
        and value is not None
        and source_evidence
        and result["meta"][key]["status"] == "needs_llm"
    ):
        result["values"][key] = value
        result["meta"][key] = {"status": "confirmed", "source": "parser", "evidence": source_evidence}


def sentence_context(text, match):
    delimiters = list(re.finditer(r"[;\n!]|\.(?=\s|$)", text))
    left = max((m.end() for m in delimiters if m.end() <= match.start()), default=0)
    right = min((m.start() for m in delimiters if m.start() >= match.end()), default=len(text))
    return text[left:match.start()], text[match.end():right]


def is_asserted(text, match):
    """Conservative lexical guard for negation, uncertainty and historical context."""
    before, after = sentence_context(text, match)
    before = before.rsplit(",", 1)[-1]
    return not (
        re.search(r"\b(?:не|нет|без|отрица\w*|исключ\w*|возмож\w*|вероят\w*|подозрен\w*|подозре\w*|риск|семейн\w*|мать|отец|матери|отца)\b|\?", before, FLAGS)
        or re.match(r"\s*(?:\?|(?:не\s+(?:выявлен\w*|обнаружен\w*|подтвержд\w*|определя\w*|установлен\w*|исключ\w*)|нет|отсутств\w*|отрица\w*|исключ\w*))", after, FLAGS)
        or re.match(r"\s*не\b", after, FLAGS)
        or not is_certain(text, match)
        or re.match(r"\s*[-–—]\s*(?:[IVX]+\b|\d)|или\b|/\s*(?:[IVX]+\b|\d)", after, FLAGS)
        or NONCURRENT.search(before + after)
    )


def is_certain(text, match):
    """Certainty guard also used for explicitly NEGATIVE statements."""
    before, after = sentence_context(text, match)
    context = before + match.group() + after
    return not re.search(
        r"\?|под\s+вопросом|нельзя\s+исключить|не\s+исключ\w*|"
        r"\b(?:возмож\w*|вероят\w*|подозрен\w*|сомнитель\w*|мать|отец|матери|отца|родствен\w*)\b",
        context, FLAGS,
    ) and not NONCURRENT.search(context)


def extract_unique(result, key, text, pattern, converter=lambda value: value, asserted=False):
    """Conflicting explicit values are unresolved; equal repeated values are OK."""
    candidates = []
    for match in re.finditer(pattern, text, FLAGS):
        if asserted and not is_asserted(text, match):
            return
        value = converter(match.group(1))
        if value is None:
            return
        candidates.append((value, evidence(text, match)))
    if candidates and all(value == candidates[0][0] for value, _ in candidates):
        confirm(result, key, *candidates[0])


def extract_dates(result, text):
    for key, label in (
        ("admission_date", r"(?:Поступил[а]?|Дата\s+(?:госпитализации|поступления))"),
        ("discharge_date", r"(?:Выписан[а]?|Дата\s+выписки)"),
    ):
        pattern = label + SEP + "(" + DATE + ")"
        period_group = 1 if key == "admission_date" else 2
        candidates = [(m.group(1), evidence(text, m)) for m in re.finditer(pattern, text, FLAGS)]
        for match in re.finditer(r"Период\s+лечения\s*:\s*с\s+(" + DATE + r")\s+по\s+(" + DATE + ")", text, FLAGS):
            candidates.append((match.group(period_group), evidence(text, match)))
        normalized = [(normalize_date(value), ev) for value, ev in candidates]
        if normalized and normalized[0][0] is not None and all(v == normalized[0][0] for v, _ in normalized):
            confirm(result, key, *normalized[0])


def extract_diagnosis(result, text):
    extract_unique(result, "diagnosis_icd", text, r"\bКод\s+МКБ[-–]?10" + SEP + r"([A-Z]\d{2}(?:\.\d{1,2})?)(?!\w|\.\d)", str.upper, asserted=True)
    classes = {"I": 1, "II": 2, "III": 3, "IV": 4, "1": 1, "2": 2, "3": 3, "4": 4}
    extract_unique(result, "killip", text, r"\bKillip\s*(?:класс\s*)?" + SEP + r"(IV|III|II|I|[1-4])\b", lambda x: classes.get(x.upper()), asserted=True)
    diagnoses = {
        "art_hyper": r"\b(?:гипертоническая\s+болезнь|артериальная\s+гипертензия)\b",
        "atr_fibril": r"\bфибрилляция\s+предсердий\b",
        "ckd": r"\b(?:ХБП|хроническая\s+болезнь\s+почек)\b",
        "copd": r"\b(?:ХОБЛ|хроническая\s+обструктивная\s+болезнь\s+л[её]гких)\b",
        "dm": r"\b(?:сахарный\s+диабет|СД)\b",
        "hf": r"\b(?:ХСН|(?:хроническая\s+|острая\s+)?сердечная\s+недостаточность)\b",
    }
    for key, pattern in diagnoses.items():
        matches = list(re.finditer(pattern, text, FLAGS))
        if matches and all(is_asserted(text, m) for m in matches):
            # For binary code fields, store as {code: 1, text: evidence}
            if key in BINARY_CODE_FIELDS:
                confirm(result, key, {"code": 1, "text": evidence(text, matches[0])}, evidence(text, matches[0]))
            else:
                confirm(result, key, 1, evidence(text, matches[0]))


def extract_exam(result, text):
    labels = {
        "bmi": r"\bИМТ", "height": r"\bРост", "weight": r"\b(?:масса\s+тела|вес)",
        "bpm": r"\b(?:пульс|ЧСС)", "rr": r"\bЧДД", "spo2": r"\bSpO[2₂]",
    }
    for key, label in labels.items():
        extract_unique(result, key, text, label + SEP + "(" + NUMBER + r")(?![\d,.]\d)", to_number, asserted=True)
    extract_unique(result, "bp", text, r"\bАД" + SEP + r"(\d{2,3}\s*/\s*\d{2,3})(?!\d)", lambda x: re.sub(r"\s", "", x), asserted=True)


def extract_smoking(result, sections):
    candidates = []
    pattern = r"\b(Бросил\s+курить|Бросила\s+курить|Прекратил(?:а)?\s+курить|Не\s+курит|Курит)\b"
    for section in sections:
        if section.kind not in {"history", "exam"}:
            continue
        text = section.text
        for match in re.finditer(pattern, text, FLAGS):
            before, after = sentence_context(text, match)
            if re.search(r"\b(?:не|никогда|мать|отец|жена|муж|родствен\w*|рекоменд\w*|совет\w*|если|возмож\w*|отрица\w*)\b|\?", before, FLAGS) or "?" in after:
                continue
            value = " ".join(match.group(1).split()).capitalize()
            code = 0 if re.match(r"(?:не\s+курит|бросил\s+курить|бросила\s+курить|прекратил(?:а)?\s+курить)$", value, FLAGS) else 1
            candidates.append(({"code": code, "text": value}, evidence(text, match)))
    if candidates and all(v == candidates[0][0] for v, _ in candidates):
        confirm(result, "smoking", *candidates[0])


def extract_ecg(result, text):
    extract_unique(result, "ecg_bpm", text, r"\bЧСС" + SEP + "(" + NUMBER + ")", to_number, asserted=True)
    rhythms = {
        "Синусовый": r"\b(?:синусовый\s+ритм|ритм\s+синусовый)\b",
        "Фибрилляция предсердий": r"\bфибрилляция\s+предсердий\b",
    }
    candidates = []
    ambiguous = False
    for value, pattern in rhythms.items():
        for match in re.finditer(pattern, text, FLAGS):
            if is_asserted(text, match):
                # For binary code field, store as {code: 1, text: value}
                candidates.append(({"code": 1, "text": value}, evidence(text, match)))
            else:
                ambiguous = True
    if candidates and not ambiguous and all(v == candidates[0][0] for v, _ in candidates):
        confirm(result, "ecg_rythm", *candidates[0])
    elevation = r"\bЭлевация\s+ST\s+в\s+отведениях\s+((?:aV[FLR]|V[1-9]|III|II|I)(?:\s*(?:[,–—-]\s*|и\s+)(?:aV[FLR]|V[1-9]|III|II|I))*)(?!\w)"
    candidates = []
    for match in re.finditer(elevation, text, FLAGS):
        if is_asserted(text, match):
            leads = re.sub(r"\s*[-—–]\s*", "–", match.group(1))
            leads = re.sub(r"\s*,\s*", ", ", leads)
            candidates.append(("Элевация ST в отведениях " + leads, evidence(text, match)))
    negative = r"\b(?:Элевации\s+ST\s+нет|(?:Элевация|Элевации)\s+ST\s+(?:не\s+(?:выявлена|выявлено)|отсутствует)|Нет\s+элевации\s+ST)\b"
    for match in re.finditer(negative, text, FLAGS):
        if not is_certain(text, match):
            return
        candidates.append(("Элевации ST нет", evidence(text, match)))
    if candidates and all(v == candidates[0][0] for v, _ in candidates):
        confirm(result, "ecg_elevation", *candidates[0])
    block = r"(?:[AА][VВ][-–\s]?блокад[аы]|атриовентрикулярная\s+блокада)"
    candidates = []
    for match in re.finditer(r"\b" + block + SEP + r"(III|II|I|[1-3])\s*(?:ст\.?|степени|степень)\b", text, FLAGS):
        if not is_asserted(text, match):
            return
        # For binary code field, store as {code: 1, text: full evidence}
        candidates.append(({"code": 1, "text": evidence(text, match)}, evidence(text, match)))
    negative_block = r"\b(?:нет\s+" + block + "|" + block + r"\s+(?:нет|отсутствует|не\s+выявлена))\b"
    for match in re.finditer(negative_block, text, FLAGS):
        if not is_certain(text, match):
            return
        candidates.append(({"code": 0, "text": NOT_SPECIFIED}, evidence(text, match)))
    if candidates and all(v == candidates[0][0] for v, _ in candidates):
        confirm(result, "ecg_avb", *candidates[0])


def extract_echo(result, text):
    for key, label, unit in (
        ("echo_ef", r"\bФВ\s+ЛЖ", "%"),
        ("echo_lvd", r"\bКДР\s+ЛЖ", "мм"),
    ):
        extract_unique(result, key, text, label + SEP + "(" + NUMBER + r")\s*" + unit, to_number, asserted=True)
    extract_unique(result, "echo_mr", text, r"\bМитральная\s+регургитация" + SEP + r"([1-4])\s*(?:ст\.?|степени|степень)\b", lambda x: {"code": 1, "text": "Митральная регургитация " + x + " ст."}, asserted=True)
    zones = r"(?:передн(?:ей|яя)\s+стенк[аи]\s+ЛЖ|нижн(?:ей|яя)\s+стенк[аи]\s+ЛЖ|боков(?:ой|ая)\s+стенк[аи]\s+ЛЖ|межжелудочков(?:ой|ая)\s+перегородк[аи]|задн(?:ей|яя)\s+стенк[аи]\s+ЛЖ|верхушк[аи]\s+ЛЖ)"
    extract_unique(result, "echo_zone", text, r"\bГипокинез\s+((?:" + zones + r"))", lambda x: {"code": 1, "text": "Гипокинез " + x}, asserted=True)


def extract_xray(result, text):
    extract_unique(result, "rg_date", text, "(" + DATE + ")", normalize_date)
    congestion = r"(?:от[её]к[а]?\s+л[её]гких|л[её]гочн(?:ый|ого)\s+засто[йя]|венозн(?:ый|ого)\s+засто[йя](?:\s+в\s+л[её]гких)?)"
    negatives = [
        r"(?:Признаков\s+)?" + congestion + r"\s+(?:не\s+(?:выявлено|выявлен|обнаружено|обнаружен)|нет|отсутствует)\b",
        r"Данных\s+за\s+" + congestion + r"\s+не\s+получено\b",
        r"\b(?:Нет|Без)\s+(?:признаков\s+)?" + congestion + r"\b",
    ]
    negative_matches = [m for p in negatives for m in re.finditer(p, text, FLAGS)]
    if any(not is_certain(text, m) for m in negative_matches):
        return
    candidates = [({"code": 0, "text": NOT_SPECIFIED}, evidence(text, m)) for m in negative_matches]
    for match in re.finditer(congestion, text, FLAGS):
        if any(m.start() <= match.start() and m.end() >= match.end() for m in negative_matches):
            continue
        if not is_asserted(text, match):
            return
        candidates.append(({"code": 1, "text": evidence(text, match)}, evidence(text, match)))
    if candidates and all(v == candidates[0][0] for v, _ in candidates):
        confirm(result, "rg_pc", *candidates[0])


def extract_coronary(result, text):
    procedure = r"(?:КАГ|коронарография|коронарографии)"
    refusal = r"\b(?:документирован\s+)?отказ\s+от\s+" + procedure + r"\b|\bотказал(?:ся|ась)\s+от\s+" + procedure + r"\b"
    completed = r"\b(?:выполнена|проведена)\s+(?:селективная\s+)?" + procedure + r"\b|\b" + procedure + r"\s+(?:выполнена|проведена)\b"
    candidates = []
    completed_matches = []
    for value, pattern in (("R", refusal), ("Y", completed)):
        for match in re.finditer(pattern, text, FLAGS):
            if not is_asserted(text, match):
                return
            candidates.append((value, evidence(text, match)))
            if value == "Y":
                completed_matches.append(match)
    if not candidates or not all(v == candidates[0][0] for v, _ in candidates):
        return
    confirm(result, "ca_fact", *candidates[0])
    if result["values"]["ca_fact"] == "Y":
        date_candidates = []
        for match in completed_matches:
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line_end = len(text) if line_end < 0 else line_end
            fragment = text[max(line_start, match.start() - 40):min(line_end, match.end() + 40)]
            before = text[max(line_start, match.start() - 40):match.start()]
            after = text[match.end():min(line_end, match.end() + 40)]
            adjacent = re.search("(" + DATE + r")\s*[.:]?\s*$", before)
            if adjacent is None:
                adjacent = re.match(r"\s*(?:от\s+)?(" + DATE + ")", after, FLAGS)
            if adjacent:
                date_candidates.append((normalize_date(adjacent.group(1)), fragment.strip()))
        if date_candidates and all(v == date_candidates[0][0] for v, _ in date_candidates):
            confirm(result, "ca_date", *date_candidates[0])


def primary_lab_text(text, admission_date, discharge_date=None):
    """First current blood panel only; ignore earlier outpatient/history panels."""
    text = REPEAT.split(text, maxsplit=1)[0]
    start = None
    has_values = False
    skip_block = False
    offset = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        if NONCURRENT.search(line):
            if start is not None:
                return text[start:line_start]
            skip_block = True
            continue
        dates = re.findall(DATE, line)
        primary = re.search(r"при\s+поступлении|первичн[а-яё]*\s+анализ", line, FLAGS)
        if primary and start is not None and has_values:
            return text[start:line_start]
        if dates:
            dates_ok = admission_date is not None and all(normalize_date(d) == admission_date for d in dates)
            if any(normalize_date(d) is None for d in dates):
                if start is not None:
                    return text[start:line_start]
                skip_block = True
                continue
            parsed_dates = [datetime.strptime(normalize_date(d), DATE_STRPTIME) for d in dates]
            admission = datetime.strptime(admission_date, DATE_STRPTIME) if admission_date else None
            discharge = datetime.strptime(discharge_date, DATE_STRPTIME) if discharge_date else None
            outside_episode = any((admission and d < admission) or (discharge and d > discharge) for d in parsed_dates)
            if start is not None and has_values:
                return text[start:line_start]
            if outside_episode or (not dates_ok and not primary):
                skip_block = True
                continue
            skip_block = False
        elif primary:
            skip_block = False
        if skip_block:
            continue
        if start is None and (primary or dates or re.search(r"результат|глюкоза|креатинин|гемоглобин|лейкоциты|тромбоциты|тропонин|холестерин|ЛПНП", line, FLAGS)):
            start = line_start
        if re.search(r"глюкоза|креатинин|гемоглобин|лейкоциты|тромбоциты|тропонин|холестерин|ЛПНП", line, FLAGS):
            has_values = True
    return text[start:] if start is not None else ""


def extract_labs(result, text):
    labels = {
        "crea": r"\bкреатинин", "glu": r"\bглюкоза", "hb": r"\bгемоглобин",
        "leucocytes": r"\bлейкоциты", "thrombocytes": r"\bтромбоциты",
        "ldl": r"\b(?:ХС[-–]ЛПНП|ЛПНП|липопротеины\s+низкой\s+плотности)",
        "tot_chol": r"\b(?:общий\s+холестерин|холестерин\s+общий)",
    }
    for key, label in labels.items():
        match = re.search(label + SEP + "(" + NUMBER + ")", text, FLAGS)
        if match and is_asserted(text, match):
            confirm(result, key, to_number(match.group(1)), evidence(text, match))
    match = re.search(r"\b(?:первый\s+)?тропониновый\s+тест" + SEP + r"(положительный|отрицательный)\b", text, FLAGS)
    if match and is_asserted(text, match):
        confirm(result, "card_trop", match.group(1).lower(), evidence(text, match))


def select_primary_labs(sections, admission_date, discharge_date):
    """Skip separately headed historical panels, then commit to one panel."""
    for section in sections:
        if section.kind != "labs":
            continue
        text = primary_lab_text(section.text, admission_date, discharge_date)
        if re.search(r"результат|глюкоза|креатинин|гемоглобин|лейкоциты|тромбоциты|тропонин|холестерин|ЛПНП", text, FLAGS):
            return text
    return ""


def extract_epicrisis_from_deidentified(text: str, template_path=TEMPLATE_PATH) -> dict[str, Any]:
    """Extract data only from text that was already deidentified."""

    result = create_empty_template(template_path)
    sections = split_sections(text)

    extract_dates(result, text)
    extract_diagnosis(result, initial_section(sections, "diagnosis"))
    extract_exam(result, initial_section(sections, "exam"))
    extract_smoking(result, sections)
    extract_ecg(result, initial_section(sections, "ecg"))
    extract_echo(result, initial_section(sections, "echo"))
    extract_xray(result, initial_section(sections, "xray"))
    extract_coronary(result, initial_section(sections, "ca"))

    labs = select_primary_labs(
        sections,
        result["values"]["admission_date"],
        result["values"]["discharge_date"],
    )
    extract_labs(result, labs)

    return result


def extract_epicrisis(text: str, template_path=TEMPLATE_PATH) -> tuple[str, dict[str, Any]]:
    """Public safe entry point: deidentify first, then extract."""

    redacted_text, deidentification = deidentify_text(text)

    record = extract_epicrisis_from_deidentified(
        redacted_text,
        template_path=template_path,
    )
    # Keep the parser record strictly {values, meta}; the audit belongs to the
    # deidentify stage and is returned separately by deidentify_text().
    return redacted_text, record


def process_all_files(input_dir=INPUT_DIR, output_dir=OUTPUT_DIR, template_path=TEMPLATE_PATH):
    """Write intermediate records only. Final JSON assembly is a later stage."""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("Input and output directories must differ")
    input_files = sorted([*input_dir.glob("*.txt"), *input_dir.glob("*.md")])
    output_dir.mkdir(parents=True, exist_ok=True)
    redacted_dir = output_dir / "redacted"
    redacted_dir.mkdir(parents=True, exist_ok=True)

    for input_file in input_files:
        raw_text = input_file.read_text(encoding="utf-8-sig")

        redacted_text, record = extract_epicrisis(raw_text, template_path=template_path)

        intermediate_file = output_dir / f"{input_file.stem}.intermediate.json"
        intermediate_file.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        redacted_file = redacted_dir / input_file.name
        redacted_file.write_text(redacted_text, encoding="utf-8")

    return len(input_files)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    cli.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    cli.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    args = cli.parse_args()
    count = process_all_files(args.input_dir, args.output_dir, args.template)
    print(f"Created {count} intermediate records")


if __name__ == "__main__":
    main()
