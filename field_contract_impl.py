"""Единый декларативный контракт 50 полей extraction pipeline.


FIELD_CONTRACT сохраняет список и порядок официального JSON-шаблона.
Все публичные описания состоят из JSON-совместимых dict/list/скаляров.
Правила описаны для будущих потребителей; текущий parser.py не изменён.


missing_value — default ФИНАЛЬНОЙ сборки, а не результат извлечения.
В промежуточном values/meta неизвестное по-прежнему равно None/needs_llm.
extractor задаёт маршрут поля, а не гарантирует успешное извлечение.
Никаких вызовов моделей, API, чтения эпикризов или финальной сборки здесь нет.
"""


from copy import deepcopy
from clinical_rules import ICD_MI_LOCALISATION, ICD_LOCALISATION_SOURCE, CORONARY_VESSELS


CONTRACT_VERSION = "2.1-case-clarifications"
NOT_SPECIFIED = "Не указано"
DATE_FORMAT = "DD.MM.YYYY"
DATE_STRPTIME = "%d.%m.%Y"
EXTRACTOR_TYPES = ("parser", "llm", "parser_or_llm")
SOURCES = {
    "field_order": "participant-output-template-v2.json",
    "formats_and_rules": "Itogovaia_tablitsa_50_polei_100_epikrizov_final.docx",
    "current_extraction": "parser.py",
}


def _rule(rule_id, description, **parameters):
    return {"id": rule_id, "description": description, **parameters}


GLOBAL_RULES = {
    "text_only": _rule("text_only", "Извлекать только информацию из текста данного эпикриза.", external_sources_allowed=False),
    "no_inference": _rule("no_inference", "Ничего не домысливать: не выводить диагноз, отсутствие болезни или показатель из симптомов, других показателей либо лечения.", medical_inference_allowed=False),
    "no_calculation": _rule("no_calculation", "Не рассчитывать BMI и другие отсутствующие показатели; принимать только явно указанные значения.", calculated_values_allowed=False),
    "date_format": _rule("date_format", "Даты нормализовать в DD.MM.YYYY без времени; проверять календарную корректность.", value_type="date", format=DATE_FORMAT, calendar_validation=True),
    "numbers_without_units": _rule("numbers_without_units", "Числа возвращать как int или float без единиц измерения; не выводить отсутствующее число.", value_type="number", python_types=["int", "float"], boolean_allowed=False, include_units=False),
    "decimal_point": _rule("decimal_point", "Десятичную запятую заменять точкой.", input_decimal_separators=[",", "."], output_decimal_separator="."),
    "primary_exam": _rule("primary_exam", "Показатели осмотра, включая bpm, брать только из первичного осмотра, не из повторного.", fields=["bmi", "bp", "bpm", "height", "rr", "spo2", "weight"], section="exam", repeat_measurements_allowed=False),
    "ecg_only": _rule("ecg_only", "ecg_bpm и остальные параметры ЭКГ брать только из записи ЭКГ, не из осмотра или диагноза.", fields=["ecg_avb", "ecg_bpm", "ecg_elevation", "ecg_rythm"], section="ecg"),
    "first_current_labs": _rule("first_current_labs", "Для лаборатории брать первый результат текущей госпитализации; контрольные, амбулаторные и прежние результаты не подставлять в пропуски первичной панели.", fields=["card_trop", "crea", "glu", "hb", "ldl", "leucocytes", "thrombocytes", "tot_chol"], section="labs", selection="first_current_hospitalization_result", fill_from_repeat_results=False),
    "coronary_applicability": _rule("coronary_applicability", "ca_lad и rca допустимы только при явно выполненной КАГ: ca_fact = Y.", fields=["ca_lad", "rca"], requires={"field": "ca_fact", "equals": "Y"}),
    "final_missing_value": _rule("final_missing_value", "Неизвестное или неоднозначное значение в финальном результате — «Не указано», кроме полей с собственным missing_value; default применяется только при финальной сборке и не подтверждает отсутствие болезни.", stage="finalization", default=NOT_SPECIFIED, per_field_override="missing_value", default_is_evidence=False),
    "medication_structure": _rule("medication_structure", "Медикаменты извлекать из назначений в эпикризе: 0 либо массив объектов с name и prescription. Если название есть, но дозировка или частота отсутствует, prescription = «Не указано». Не заменять массив бинарным 1.", value_type="medication_list_or_zero", section=["recommendations", "treatment"], required_item_keys=["name", "prescription"], missing_prescription=NOT_SPECIFIED, scalar_values=[0]),
    "section_scope": _rule("section_scope", "Не расширять поиск показателя, привязанного к разделу, на весь документ при отсутствии этого раздела.", whole_document_fallback=False),
    "negation_and_conflict": _rule("negation_and_conflict", "Учитывать отрицания, предположения и конфликтующие значения; неоднозначность оставлять нерешённой.", ambiguous_values_policy="unresolved"),
    "intermediate_unknown": _rule("intermediate_unknown", "На этапе извлечения неизвестное сохранять как None с needs_llm; финальные defaults пока не применять, независимо от extractor.", stage="extraction", value=None, status="needs_llm", source=None, evidence=None, apply_final_defaults=False),
    "parser_evidence": _rule("parser_evidence", "Для подтверждённого парсером значения сохранять небольшой исходный фрагмент evidence.", status="confirmed", source="parser", evidence_required=True, evidence_kind="verbatim_source_fragment"),
    "deidentification": _rule(
        "deidentification",
        "До передачи текста модели удаляются ФИО пациента, дата рождения, возраст, "
        "номер истории болезни, номер медицинской карты и номер отделения. "
        "Маркеры [REDACTED_...] не являются медицинскими значениями.",
        stage="preprocessing",
        raw_identifiers_allowed=False,
    ),
    "binary_code_with_text": _rule(
        "binary_code_with_text",
        "Для бинарных полей (0/1) возвращать объект {code, text}. "
        "На стадии parser code=0 подтверждать только при явном отрицании; отсутствие упоминания оставлять None/needs_llm. "
        "На стадии LLM отсутствие может получить missing_value только по правилу конкретного поля; text = «Не указано». "
        "code = 1 при прямом указании признака; text = точная формулировка из таблицы вариантов.",
        value_type="binary_code_object",
        code_values=[0, 1],
        text_required_for_code_1=True,
    ),
}


_COMMON_RULE_IDS = [
    "text_only", "no_inference", "no_calculation", "final_missing_value",
    "negation_and_conflict", "intermediate_unknown", "parser_evidence",
    "deidentification",
]


def _field(name, extractor, value_type, description, rules, *,
           missing_value=NOT_SPECIFIED, global_rule_ids=(), **details):
    return {
        "name": name,
        "extractor": extractor,
        "value_type": value_type,
        "missing_value": missing_value,
        "description": description,
        "rules": rules,
        "global_rule_ids": [*_COMMON_RULE_IDS, *global_rule_ids],
        **details,
    }


def _date(name, description, rules, *, section=None, **details):
    return _field(name, "parser", "date", description, rules,
                  global_rule_ids=["date_format", *(["section_scope"] if section else [])],
                  format=DATE_FORMAT, pattern=r"^\d{2}\.\d{2}\.\d{4}$",
                  calendar_validation=True, section=section, **details)


def _number(name, description, rules, *, section, extractor="parser", **details):
    scopes = ["numbers_without_units", "decimal_point", "section_scope"]
    if section == "exam":
        scopes.append("primary_exam")
    elif section == "labs":
        scopes.append("first_current_labs")
    elif section == "ecg":
        scopes.append("ecg_only")
    return _field(name, extractor, "number", description, rules,
                  global_rule_ids=scopes, section=section, **details)


def _enum(name, extractor, description, allowed_values, rules, *, section,
          missing_value=NOT_SPECIFIED, global_rule_ids=(), **details):
    return _field(name, extractor, "enum", description, rules,
                  missing_value=missing_value, allowed_values=list(allowed_values),
                  enum_value_type="integer" if all(type(v) is int for v in allowed_values) else "string",
                  global_rule_ids=["section_scope", *global_rule_ids], section=section, **details)


def _binary_code(name, extractor, description, rules, *, section, allowed_texts, **details):
    """Бинарное поле с кодом 0/1 и текстовой формулировкой."""
    extra_global_rule_ids = [*details.pop("global_rule_ids", [])]
    return _field(
        name, extractor, "binary_code", description, rules,
        missing_value={"code": 0, "text": NOT_SPECIFIED},
        global_rule_ids=["binary_code_with_text", "section_scope", *extra_global_rule_ids, *details.pop("extra_global_rule_ids", [])],
        section=section,
        code_values=[0, 1],
        allowed_texts=allowed_texts,
        item_schema={
            "type": "object",
            "required": ["code", "text"],
            "additionalProperties": False,
            "properties": {
                "code": {"type": "integer", "enum": [0, 1]},
                "text": {"type": "string", "minLength": 1},
            },
        },
        **details
    )


def _diagnosis_flag(name, description, explicit_terms, allowed_texts):
    return _binary_code(name, "parser_or_llm", description, [
        _rule("explicit_diagnosis", "Парсер подтверждает code=1 только при явном, неотрицаемом диагнозе в разделе диагноза.", parser_confirmed_values=[1], explicit_terms=explicit_terms),
        _rule("no_absence_from_silence", "Неупоминание диагноза не подтверждает code=1. Отрицания и неоднозначные формулировки парсер оставляет needs_llm.", absence_of_mention_is_negative=False),
    ], section="diagnosis", allowed_texts=allowed_texts)


def _lab_number(name, description, labels):
    return _number(name, description, [
        _rule("explicit_lab_value", "Извлекать явно указанное числовое значение данного показателя без единиц; не вычислять.", labels=labels),
        _rule("initial_panel_only", "Брать первое значение текущей госпитализации из первичной панели; не подменять контрольным результатом.", selection="first_current_hospitalization_result"),
    ], section="labs")


def _medication(name, description, specific_rule):
    return _field(name, "llm", "medication_list_or_zero", description, [
        _rule("deferred_medication_classification", "В текущем deterministic parser поле намеренно отложено; классификация названий препаратов требует следующего этапа.", deterministic_extraction_enabled=False),
        _rule("medication_group", specific_rule),
        _rule("retain_prescription", "Сохранить название и текст дозировки/количества и частоты. Если дозировка или частота отсутствует, prescription = «Не указано». Каждый препарат — отдельный объект; несколько назначений допустимы.", missing_prescription=NOT_SPECIFIED),
    ], missing_value=0, global_rule_ids=["medication_structure", "section_scope"],
        section=["recommendations", "treatment"], scalar_values=[0], item_schema={
            "type": "object", "required": ["name", "prescription"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "prescription": {"type": "string", "minLength": 1},
            },
        })


# Explicit entries follow the official template exactly. Tests detect schema
# drift; no runtime dependency on the parser, DOCX reader or external services.
FIELD_CONTRACT = {
    "admission_date": _date("admission_date", "Дата госпитализации / поступления.", [
        _rule("explicit_admission_date", "Извлекать дату из «Поступил/Поступила», «Дата госпитализации/поступления» либо начала явно указанного периода лечения; не брать дату рождения.", period_endpoint="start", exclude_birth_date=True),
    ]),
    "discharge_date": _date("discharge_date", "Дата выписки.", [
        _rule("explicit_discharge_date", "Извлекать дату из «Выписан/Выписана», «Дата выписки» либо конца явно указанного периода лечения; не брать дату оформления вместо даты выписки.", period_endpoint="end"),
    ]),
    "art_hyper": _diagnosis_flag("art_hyper", "Артериальная гипертензия / гипертоническая болезнь.", 
        ["Гипертоническая болезнь", "Артериальная гипертензия"],
        allowed_texts=[
            "Гипертоническая болезнь",
            "ГБ I стадии",
            "ГБ II стадии",
            "ГБ III стадии",
            "ГБ I ст.",
            "ГБ II ст.",
            "ГБ III ст.",
            "Артериальная гипертензия",
        ]
    ),
    "atr_fibril": _diagnosis_flag("atr_fibril", "Фибрилляция предсердий как диагноз.", 
        ["Фибрилляция предсердий"],
        allowed_texts=[
            "Фибрилляция предсердий",
            "Фибрилляция предсердий, постоянная форма",
        ]
    ),
    "ckd": _diagnosis_flag("ckd", "Хроническая болезнь почек.", 
        ["ХБП", "Хроническая болезнь почек"],
        allowed_texts=[
            "ХБП",
            "ХБП 1",
            "ХБП 2",
            "ХБП 3А",
            "ХБП 3Б",
            "Хроническая болезнь почек",
        ]
    ),
    "copd": _diagnosis_flag("copd", "ХОБЛ.", 
        ["ХОБЛ", "Хроническая обструктивная болезнь лёгких"],
        allowed_texts=[
            "ХОБЛ",
            "ХОБЛ вне обострения",
            "Хроническая обструктивная болезнь лёгких",
        ]
    ),
    "diagnosis_icd": _field("diagnosis_icd", "parser", "string", "Явно написанный код диагноза по МКБ-10.", [
        _rule("explicit_icd_code", "Извлекать только явно указанный код МКБ-10; не устанавливать код по названию заболевания. Конкурирующие коды без однозначного выбора оставлять нерешёнными."),
    ], global_rule_ids=["section_scope"], section="diagnosis", format="ICD-10",
        pattern=r"^[A-Z]\d{2}(?:\.\d{1,2})?$", example="I21.0",
        open_questions=["Уточнить формат нескольких кодов и правило выбора основного диагноза на датасете."]),
    "dm": _diagnosis_flag("dm", "Сахарный диабет.", 
        ["Сахарный диабет", "СД"],
        allowed_texts=[
            "Сахарный диабет 1 типа",
            "Сахарный диабет 2 типа",
            "СД 1 типа",
            "СД 2 типа",
        ]
    ),
    "hf": _diagnosis_flag("hf", "Хроническая сердечная недостаточность.", 
        ["ХСН", "Сердечная недостаточность"],
        allowed_texts=[
            "ХСН",
            "ХСН 1",
            "ХСН 2А",
            "ХСН 2Б",
            "ХСН 1, ФК 2",
            "ХСН 2А, ФК 2",
            "ХСН 2Б, ФК 3",
            "Сердечная недостаточность",
        ]
    ),
    "killip": _enum("killip", "parser", "Явно указанный класс Killip.", [1, 2, 3, 4], [
        _rule("explicit_killip", "Принимать только явно указанный класс Killip; римские I–IV нормализовать в 1–4. Не определять класс по симптомам или другим данным.", normalization={"I": 1, "II": 2, "III": 3, "IV": 4}),
    ], section="diagnosis"),
    "mi_localisation": _binary_code("mi_localisation", "llm", "Локализация инфаркта миокарда.", [
        _rule("localisation_priority", "Приоритет: явная локализация инфаркта в диагнозе, затем только централизованный ICD fallback, затем missing_value. Не использовать ЭКГ или анатомию сосудов. Не классифицировать неизвестные ICD самостоятельно.", priority=["explicit_diagnosis", "diagnosis_icd", "missing_value"], icd_mapping=ICD_MI_LOCALISATION, codebook_source=ICD_LOCALISATION_SOURCE, excluded_sources=["ecg", "ca"]),
    ], section="diagnosis", allowed_texts=[
        "передней стенки левого желудочка",
        "передней стенки",
        "нижней стенки левого желудочка",
        "нижней стенки",
        "боковой стенки левого желудочка",
        "боковой стенки",
        "задней стенки левого желудочка",
        "задней стенки",
        "межжелудочковая перегородка",
        "правый желудочек",
    ]),
    "tlt": _binary_code("tlt", "llm", "Тромболитическая терапия.", [
        _rule("deferred_tlt", "Парсер не извлекает ТЛТ. На следующем этапе учитывать документированную терапию; не выводить её факт из диагноза или общих описаний лечения.", deterministic_extraction_enabled=False),
    ], section="treatment", allowed_texts=[
        "На догоспитальном этапе проведён системный тромболизис",
        "Тромболизис",
        "ТЛТ",
    ], open_questions=["В DOCX точные критерии документирования ТЛТ требуют уточнения на датасете."]),
    "type_acs": _binary_code("type_acs", "llm", "Тип острого коронарного синдрома.", [
        _rule("deferred_acs_type", "Парсер не определяет type_acs. На следующем этапе нормализовать формулировку диагноза в допустимую категорию, если текст позволяет однозначное сопоставление; не устанавливать тип ОКС самостоятельно по элевации ST.", deterministic_extraction_enabled=False),
    ], section="diagnosis", allowed_texts=[
        "ИБС. Острый инфаркт миокарда передней стенки левого желудочка с подъёмом ST.",
        "Острый инфаркт миокарда передней стенки, с элевацией ST.",
        "ИБС. Острый инфаркт миокарда нижней стенки левого желудочка с подъёмом ST.",
        "Острый инфаркт миокарда нижней стенки, с элевацией ST.",
        "ИБС. Острый инфаркт миокарда боковой стенки левого желудочка с подъёмом ST.",
        "Острый инфаркт миокарда боковой стенки, с элевацией ST.",
        "ИБС. Острый инфаркт миокарда без подъёма ST.",
        "Острый коронарный синдром без подъёма ST, инфаркт миокарда.",
        "ИБС. Нестабильная стенокардия.",
        "ИБС: прогрессирующая стенокардия, нестабильное течение.",
    ]),
    "bmi": _number("bmi", "Явно указанный индекс массы тела (ИМТ).", [
        _rule("explicit_bmi", "Брать только явно записанный ИМТ из первичного осмотра; не рассчитывать по росту и весу.", labels=["ИМТ"], calculate_from_height_weight=False),
    ], section="exam"),
    "bp": _field("bp", "parser", "string", "Артериальное давление при первичном осмотре.", [
        _rule("explicit_bp", "Сохранить явно записанную пару АД строкой через / без пробелов и единиц, например 140/90. Не брать повторный осмотр.", labels=["АД"], example="140/90"),
    ], global_rule_ids=["primary_exam", "section_scope"], section="exam",
        format="NNN/NN", pattern=r"^\d{2,3}/\d{2,3}$"),
    "bpm": _number("bpm", "Частота пульса / ЧСС при поступлении.", [
        _rule("exam_pulse", "Брать явно указанный пульс или ЧСС из первичного осмотра; не использовать ЧСС записи ЭКГ.", labels=["пульс", "ЧСС"]),
    ], section="exam"),
    "height": _number("height", "Рост пациента.", [
        _rule("explicit_height", "Брать явно указанный рост из первичного осмотра без единиц измерения.", labels=["Рост"]),
    ], section="exam"),
    "rr": _number("rr", "Частота дыхательных движений.", [
        _rule("explicit_rr", "Брать явно указанную ЧДД из первичного осмотра.", labels=["ЧДД"]),
    ], section="exam"),
    "smoking": _binary_code("smoking", "parser_or_llm", "Статус курения пациента.", [
        _rule("explicit_smoking", "Парсер принимает только явные фразы «Не курит», «Курит», «Бросил курить» в анамнезе или осмотре; отрицание и отказ от курения не распознавать как «Курит».", explicit_terms=["Не курит", "Курит", "Бросил курить"]),
        _rule("patient_smoking_only", "Не использовать курение родственников, советы бросить курить или неоднозначные описания; сложную формулировку оставить следующему этапу."),
    ], section=["history", "exam"], allowed_texts=[
        "Курит",
        "Не курит",
        "Бросил курить",
        "Прекратила курить",
    ]),
    "spo2": _number("spo2", "Периферическая сатурация кислорода (SpO₂).", [
        _rule("explicit_spo2", "Брать явно указанную SpO2 / SpO₂ из первичного осмотра, без знака %.", labels=["SpO2", "SpO₂"]),
    ], section="exam"),
    "weight": _number("weight", "Масса тела / вес пациента.", [
        _rule("explicit_weight", "Брать явно указанную массу тела или вес из первичного осмотра без единиц измерения.", labels=["масса тела", "вес"]),
    ], section="exam"),
    "ecg_avb": _binary_code("ecg_avb", "parser_or_llm", "Атриовентрикулярная блокада по ЭКГ.", [
        _rule("explicit_av_block", "Парсер принимает явную AV-блокаду с указанной степенью I, II или III либо явное отсутствие AV-блокады; одного названия блокады недостаточно.", normalization={"I": 1, "II": 2, "III": 3}, explicit_absence_value=0),
    ], section="ecg", global_rule_ids=["ecg_only"], allowed_texts=[
        "АВ-блокада I степени",
        "АВ-блокада II степени",
        "АВ-блокада III степени",
        "Атриовентрикулярная блокада I степени",
        "Атриовентрикулярная блокада II степени",
        "Атриовентрикулярная блокада III степени",
    ]),
    "ecg_bpm": _number("ecg_bpm", "ЧСС по записи ЭКГ.", [
        _rule("explicit_ecg_rate", "Извлекать явно указанную ЧСС только из ЭКГ.", labels=["ЧСС"]),
    ], section="ecg"),
    "ecg_elevation": _field("ecg_elevation", "parser_or_llm", "string", "Подъём сегмента ST на ЭКГ.", [
        _rule("explicit_st_elevation", "При явно названных отведениях сохранить «Элевация ST в отведениях …» с нормализованными разделителями; при явном отрицании — «Элевации ST нет». Не определять по диагнозу.", positive_prefix="Элевация ST в отведениях ", explicit_absence_value="Элевации ST нет", normalize_range_separator="–"),
    ], global_rule_ids=["ecg_only", "section_scope"], section="ecg",
        format="Элевация ST в отведениях … | Элевации ST нет",
        open_questions=["В DOCX степень нормализации перечисления отведений отмечена как требующая проверки."]),
    "ecg_rythm": _binary_code("ecg_rythm", "parser_or_llm", "Ритм сердца по ЭКГ.", [
        _rule("literal_ecg_rhythm", "Парсер принимает только явно названную разрешённую категорию ритма; «синусовый ритм» нормализуется в «Синусовый». Сложные или конфликтующие описания не классифицировать самостоятельно."),
    ], section="ecg", global_rule_ids=["ecg_only"], allowed_texts=[
        "синусовый ритм",
        "фибрилляция предсердий",
    ]),
    "echo_ef": _number("echo_ef", "Фракция выброса левого желудочка по ЭхоКГ.", [
        _rule("explicit_echo_ef", "Брать явно указанную ФВ ЛЖ из ЭхоКГ без знака %.", labels=["ФВ ЛЖ"]),
    ], section="echo"),
    "echo_lvd": _number("echo_lvd", "Конечный диастолический размер левого желудочка (КДР ЛЖ).", [
        _rule("explicit_lvd_only", "Брать только явно указанный КДР ЛЖ в мм из ЭхоКГ; не подставлять КСР, размер ЛП или другой размер ЛЖ.", labels=["КДР ЛЖ"], source_unit="мм", substitute_other_dimensions=False),
    ], section="echo"),
    "echo_lvd_2": _number("echo_lvd_2", "Конечно-систолический размер левого желудочка (КСР ЛЖ).", [
        _rule("explicit_ksr_only", "Брать только явно указанный КСР ЛЖ в мм из ЭхоКГ; не подставлять КДР или другие размеры.", labels=["КСР ЛЖ"], source_unit="мм", substitute_other_dimensions=False),
    ], section="echo", extractor="llm",
        open_questions=["Точный второй параметр ЛЖ и семантика допустимого числового 0 требуют уточнения; не считать 0 default."]),
    "echo_mr": _binary_code("echo_mr", "parser", "Митральная регургитация по ЭхоКГ.", [
        _rule("explicit_mr_degree", "Брать только явно указанную степень митральной регургитации из ЭхоКГ; не выводить степень из описания клапана."),
    ], section="echo", allowed_texts=[
        "Митральная регургитация 1 ст.",
        "Митральная регургитация 2 ст.",
        "Митральная регургитация 3 ст.",
        "Митральная регургитация 4 ст.",
    ]),
    "echo_zone": _binary_code("echo_zone", "parser_or_llm", "Зона нарушения локальной сократимости по ЭхоКГ.", [
        _rule("explicit_wall_motion", "Парсер принимает только явное упоминание одной разрешённой категории в ЭхоКГ; не выводить тип нарушения из иных описаний сократимости."),
    ], section="echo", allowed_texts=[
        "Гипокинез передней стенки ЛЖ",
        "Гипокинез нижней стенки ЛЖ",
        "Гипокинез боковой стенки ЛЖ",
        "Гипокинез межжелудочковой перегородки",
        "Гипокинез задней стенки ЛЖ",
        "Гипокинез верхушки ЛЖ",
    ]),
    "rg_date": _date("rg_date", "Дата рентгенографии грудной клетки.", [
        _rule("explicit_xray_date", "Извлекать дату исследования только из раздела рентгенографии; конфликтующие даты без однозначного выбора оставлять нерешёнными."),
    ], section="xray"),
    "rg_pc": _binary_code("rg_pc", "parser_or_llm", "Лёгочный / венозный застой или отёк лёгких по рентгенографии ОГК.", [
        _rule("explicit_congestion", "Подтверждать code=1 только при явном описании застоя/отёка, code=0 — при явном отрицании венозного/лёгочного застоя или отёка. Отсутствие упоминания остаётся unresolved до финальных defaults.", explicit_presence_value=1, explicit_absence_value=0),
        _rule("no_congestion_from_silence", "Неупоминание или неоднозначная формулировка не подтверждает code=1; финальный default code=0 сохраняется отдельно от evidence."),
    ], section="xray", allowed_texts=[
        "Признаки венозного застоя в лёгких",
        "Умеренный венозный застой в малом круге кровообращения",
        "Признаки интерстициального отёка лёгких",
        "Признаков отёка лёгких не выявлено",
        "Данных за венозный застой в лёгких не получено",
    ]),
    "ca_date": _date("ca_date", "Дата выполненной коронарографии.", [
        _rule("completed_ca_date", "Дата допустима только при явно выполненной КАГ. Парсер берёт дату рядом с утверждением выполнения, а не дату планирования, согласия или поступления.", requires={"field": "ca_fact", "equals": "Y"}),
    ], section="ca", applicable_when={"field": "ca_fact", "equals": "Y"}),
    "ca_fact": _enum("ca_fact", "parser_or_llm", "Факт / статус коронарографии.", ["Y", "R", "N"], [
        _rule("explicit_ca_status", "Y — явно выполнена КАГ; R — документирован отказ; N — сведений нет при финальной сборке. Упоминание КАГ или рекомендация её выполнить не подтверждает Y.", meanings={"Y": "выполнена", "R": "документирован отказ", "N": "сведений нет"}, parser_confirmed_values=["Y", "R"]),
    ], section="ca", missing_value="N"),
    "ca_lad": _enum("ca_lad", "llm", "Поражение LAD / ПМЖВ по выполненной коронарографии.", [0, 1, 2], [
        _rule("deferred_lad", "Парсер не интерпретирует поражение сосудов. Следующий этап использует только явно описанное поражение LAD/ПМЖВ.", deterministic_extraction_enabled=False),
        _rule("no_significant_stenosis", "Явное «гемодинамически значимого стеноза нет» для LAD/ПМЖВ означает категорию 0; не интерпретировать как тяжёлый стеноз.", vessel_aliases=CORONARY_VESSELS["ca_lad"], explicit_value=0),
        _rule("stenosis_categories", "Категории DOCX: 0 — <50%; 1 — 50–89%; 2 — ≥90% или окклюзия; без применимого значения — «Не указано».", categories=[{"value": 0, "percent_less_than": 50}, {"value": 1, "percent_minimum": 50, "percent_maximum": 89}, {"value": 2, "percent_minimum": 90, "or_explicit_occlusion": True}]),
    ], section="ca", global_rule_ids=["coronary_applicability"],
        applicable_when={"field": "ca_fact", "equals": "Y"},
        open_questions=["Выбор при нескольких стенозах и значения между 89% и 90% контрактом DOCX не уточнены; не вводить округление или собственное правило."]),
    "rca": _enum("rca", "llm", "Поражение RCA / ПКА по выполненной коронарографии.", [0, 1, 2], [
        _rule("deferred_rca", "Парсер не интерпретирует поражение сосудов. Следующий этап использует только явно описанное поражение RCA/ПКА.", deterministic_extraction_enabled=False),
        _rule("no_significant_stenosis", "Явное «гемодинамически значимого стеноза нет» для RCA/ПКА означает категорию 0; не интерпретировать как тяжёлый стеноз.", vessel_aliases=CORONARY_VESSELS["rca"], explicit_value=0),
        _rule("stenosis_categories", "Категории DOCX: 0 — <50%; 1 — 50–89%; 2 — ≥90% или окклюзия; без применимого значения — «Не указано».", categories=[{"value": 0, "percent_less_than": 50}, {"value": 1, "percent_minimum": 50, "percent_maximum": 89}, {"value": 2, "percent_minimum": 90, "or_explicit_occlusion": True}]),
    ], section="ca", global_rule_ids=["coronary_applicability"],
        applicable_when={"field": "ca_fact", "equals": "Y"},
        open_questions=["Выбор при нескольких стенозах и значения между 89% и 90% контрактом DOCX не уточнены; не вводить округление или собственное правило."]),
    "card_trop": _enum("card_trop", "parser_or_llm", "Результат первого тропонинового теста текущей госпитализации.", ["положительный", "отрицательный"], [
        _rule("explicit_first_troponin", "Парсер принимает явные «первый тропониновый тест — положительный/отрицательный» из первичных анализов. Не классифицировать числовой тропонин по порогам и не заменять первым найденным контрольным тестом.", classify_numeric_troponin=False),
    ], section="labs", global_rule_ids=["first_current_labs"]),
    "crea": _lab_number("crea", "Креатинин.", ["креатинин"]),
    "glu": _lab_number("glu", "Глюкоза.", ["глюкоза"]),
    "hb": _lab_number("hb", "Гемоглобин.", ["гемоглобин"]),
    "ldl": _lab_number("ldl", "Липопротеины низкой плотности / ЛПНП.", ["ХС-ЛПНП", "ЛПНП", "липопротеины низкой плотности"]),
    "leucocytes": _lab_number("leucocytes", "Лейкоциты.", ["лейкоциты"]),
    "thrombocytes": _lab_number("thrombocytes", "Тромбоциты.", ["тромбоциты"]),
    "tot_chol": _lab_number("tot_chol", "Общий холестерин.", ["общий холестерин", "холестерин общий"]),
    "2_aag": _medication("2_aag", "Двойная антиагрегантная терапия / два антиагреганта.", "Сохранять каждый антиагрегант отдельным объектом массива. Не сводить комбинацию препаратов к бинарному признаку; условие двойной терапии и список P2Y12 требуют уточнения по DOCX."),
    "ace_ing_sartan": _medication("ace_ing_sartan", "Ингибитор АПФ / сартан.", "Выделять из назначений препараты соответствующей группы; при нескольких назначениях сохранять массив, не выбирать один препарат произвольно."),
    "anticoagulant": _medication("anticoagulant", "Антикоагулянт.", "Выделять из назначений антикоагулянты с исходными названиями и назначениями."),
    "aspirin": _medication("aspirin", "Аспирин / ацетилсалициловая кислота.", "Выделять из назначений аспирин/АСК, сохраняя указанное в тексте название и назначение."),
    "bb": _medication("bb", "Бета-адреноблокатор.", "Выделять из назначений бета-адреноблокаторы с исходными названиями и назначениями."),
    "statin": _medication("statin", "Статин.", "Выделять из назначений статины с исходными названиями и назначениями."),
}


FIELD_NAMES = tuple(FIELD_CONTRACT)
FIELDS_BY_EXTRACTOR = {
    extractor: [name for name, field in FIELD_CONTRACT.items() if field["extractor"] == extractor]
    for extractor in EXTRACTOR_TYPES
}
DEFERRED_FIELDS = tuple(FIELDS_BY_EXTRACTOR["llm"])
MEDICATION_FIELDS = tuple(
    name for name, field in FIELD_CONTRACT.items()
    if field["value_type"] == "medication_list_or_zero"
)
BINARY_CODE_FIELDS = tuple(
    name for name, field in FIELD_CONTRACT.items()
    if field["value_type"] == "binary_code"
)


def get_contract():
    """Return an independent JSON-serializable snapshot for a future consumer."""
    return deepcopy({
        "version": CONTRACT_VERSION,
        "sources": SOURCES,
        "global_rules": GLOBAL_RULES,
        "fields": FIELD_CONTRACT,
    })
