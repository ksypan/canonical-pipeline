"""One contract-to-schema translation shared by LLM and final validation."""
import json
import math
from copy import deepcopy
from datetime import datetime
from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from field_contract import FIELD_CONTRACT, DATE_STRPTIME, NOT_SPECIFIED
from pipeline_errors import (
    ConfigurationError, InvalidRequestError, UnexpectedResponseError,
    ExtraFieldsError, MissingFieldsError, SchemaViolationError, InvalidLLMResponseError,
)

# Python/JSON consumers require actual integers for codes, not 1.0 or True.
ContractValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda checker, value: type(value) is int))

def _json_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "number"
    if isinstance(value, str):
        return "string"
    raise ConfigurationError("Контракт содержит неподдерживаемый missing_value/enum.")


def _missing_schema(value):
    return {"type": _json_type(value), "enum": [value]}


def _field_schema(descriptor, *, strict_binary_text=True):
    kind = descriptor["value_type"]
    missing = descriptor["missing_value"]
    if kind == "enum":
        allowed = deepcopy(descriptor.get("allowed_values"))
        if not isinstance(allowed, list) or not allowed:
            raise ConfigurationError("Enum-поле должно иметь allowed_values.")
        types = {_json_type(value) for value in allowed}
        if len(types) != 1:
            raise ConfigurationError("Enum должен иметь один базовый тип.")
        base_type = types.pop()
        base = {"type": base_type, "enum": allowed}
        if _json_type(missing) == base_type:
            if missing not in allowed:
                base["enum"].append(missing)
            schema = base
        else:
            schema = {"anyOf": [base, _missing_schema(missing)]}
    elif kind in {"number", "integer"}:
        base = {"type": kind}
        missing_type = _json_type(missing)
        already_allowed = missing_type == kind or (kind == "number" and missing_type == "integer")
        schema = base if already_allowed else {"anyOf": [base, _missing_schema(missing)]}
    elif kind in {"string", "date"}:
        base = {"type": "string"}
        for contract_key, schema_key in (("pattern", "pattern"), ("min_length", "minLength"), ("max_length", "maxLength")):
            if contract_key in descriptor:
                base[schema_key] = deepcopy(descriptor[contract_key])
        # Missing sentinel bypasses date/code patterns; it is never coerced
        # into a date or a numeric string.
        schema = base if len(base) == 1 and isinstance(missing, str) else {"anyOf": [base, _missing_schema(missing)]}
    elif kind == "binary_code":
        item = deepcopy(descriptor["item_schema"])
        if item.get("type") != "object" or set(item.get("properties", {})) != {"code", "text"}:
            raise ConfigurationError("Binary item_schema must contain code/text only.")
        item["required"] = ["code", "text"]
        item["additionalProperties"] = False
        zero = deepcopy(item)
        zero["properties"]["code"] = {"type": "integer", "enum": [0]}
        zero["properties"]["text"] = {"type": "string", "enum": [descriptor["missing_value"]["text"]]}
        if not strict_binary_text and descriptor.get("name") == "smoking":
            # The fresh parser confirms explicit negative smoking evidence as
            # code=0 and keeps its verbatim text. This final-stage exception
            # preserves that evidence; the LLM schema remains strict.
            zero["properties"]["text"] = {"type": "string", "anyOf": [
                {"enum": [descriptor["missing_value"]["text"]]},
                {"pattern": r"^(?:Не курит|Бросил(?:а)? курить|Прекратил(?:а)? курить)$"},
            ]}
        one = deepcopy(item)
        one["properties"]["code"] = {"type": "integer", "enum": [1]}
        if strict_binary_text and descriptor.get("allowed_texts"):
            one["properties"]["text"]["enum"] = deepcopy(descriptor["allowed_texts"])
        schema = {"anyOf": [zero, one]}
    elif kind == "medication_list_or_zero":
        scalars = deepcopy(descriptor.get("scalar_values"))
        item = deepcopy(descriptor.get("item_schema"))
        if scalars != [0] or not isinstance(item, dict):
            raise ConfigurationError("Для медикаментов нужен контракт 0 либо массив объектов.")
        if item.get("type") != "object" or set(item.get("properties", {})) != {"name", "prescription"}:
            raise ConfigurationError("Схема назначения должна содержать только name и prescription.")
        item["required"] = list(item["properties"])
        item["additionalProperties"] = False
        schema = {"anyOf": [
            {"type": "integer", "enum": scalars},
            {"type": "array", "minItems": 1, "items": item},
        ]}
        if type(missing) is not int or missing != 0:
            schema["anyOf"].append(_missing_schema(missing))
    else:
        raise ConfigurationError("Неподдерживаемый value_type в FIELD_CONTRACT.")
    if descriptor.get("description"):
        schema["description"] = descriptor["description"]
    return schema


def build_response_schema(target_fields, field_contract=FIELD_CONTRACT, *, strict_binary_text=True):
    """Translate target contract entries; do not encode medical dependencies.

    Schema controls the shape and simple types. Cross-field clinical rules,
    calendar correctness and other business checks belong to the later stage.
    """
    if not isinstance(target_fields, (list, tuple)) or any(not isinstance(name, str) for name in target_fields):
        raise InvalidRequestError("target_fields должен быть списком имён полей.")
    if len(set(target_fields)) != len(target_fields):
        raise InvalidRequestError("target_fields содержит повторяющиеся поля.")
    properties = {}
    for name in target_fields:
        if name not in field_contract:
            raise InvalidRequestError("target_fields содержит неизвестное контракту поле.")
        descriptor = field_contract[name]
        if not {"value_type", "missing_value"}.issubset(descriptor):
            raise ConfigurationError("Неполное описание поля в FIELD_CONTRACT.")
        properties[name] = _field_schema(descriptor, strict_binary_text=strict_binary_text)
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(target_fields),
        "additionalProperties": False,
    }
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ConfigurationError("FIELD_CONTRACT создаёт некорректную JSON Schema.") from None
    return schema


def validate_response(result, target_fields, schema, field_contract=FIELD_CONTRACT):
    if not isinstance(result, dict):
        raise UnexpectedResponseError("Ответ модели должен быть JSON-объектом.")
    actual, expected = set(result), set(target_fields)
    if actual - expected:
        raise ExtraFieldsError("Ответ модели содержит лишние поля.")
    if expected - actual:
        raise MissingFieldsError("В ответе модели отсутствуют обязательные целевые поля.")
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise InvalidLLMResponseError("Ответ содержит значение, недопустимое в JSON.") from None
    try:
        Draft202012Validator.check_schema(schema)
        ContractValidator(schema).validate(result)
    except SchemaError:
        raise ConfigurationError("Некорректная JSON Schema ответа.") from None
    except ValidationError:
        # Standard error messages can include the whole model value. Keep
        # public errors fixed so model content/credentials cannot leak.
        raise SchemaViolationError("Ответ модели нарушает JSON Schema контракта.") from None
    for name in target_fields:
        descriptor = field_contract[name]
        value = result[name]
        if descriptor["value_type"] == "date" and value != descriptor["missing_value"]:
            try:
                datetime.strptime(value, DATE_STRPTIME)
            except (ValueError, TypeError):
                raise SchemaViolationError("Invalid calendar date: " + name) from None
        if descriptor["value_type"] == "binary_code" and not value["text"].strip():
            raise SchemaViolationError("Empty binary text: " + name)
        if descriptor["value_type"] == "binary_code" and value["text"].casefold() == NOT_SPECIFIED.casefold():
            if value["text"] != NOT_SPECIFIED or value["code"] != 0:
                raise SchemaViolationError("Invalid binary missing marker: " + name)
        if descriptor["value_type"] == "medication_list_or_zero" and isinstance(value, list):
            if any(not item[key].strip() for item in value for key in ("name", "prescription")):
                raise SchemaViolationError("Empty medication text: " + name)
            if any(item["prescription"].casefold() == NOT_SPECIFIED.casefold() and item["prescription"] != NOT_SPECIFIED for item in value):
                raise SchemaViolationError("Invalid medication missing marker: " + name)
    return result


def build_evidence_response_schema(target_fields, field_contract=FIELD_CONTRACT):
    """Internal structured output; official values stay a separate object."""
    from evidence_layer import MAX_EVIDENCE_LENGTH
    values_schema = build_response_schema(target_fields, field_contract)
    return {
        "type": "object", "required": ["values", "evidence"], "additionalProperties": False,
        "properties": {
            "values": values_schema,
            "evidence": {"type": "object", "required": list(target_fields), "additionalProperties": False,
                         "properties": {name: {"anyOf": [{"type": "string", "minLength": 1,
                                                            "maxLength": MAX_EVIDENCE_LENGTH}, {"type": "null"}]}
                                        for name in target_fields}},
        },
    }


def split_llm_response(payload, target_fields, *, evidence_mode=False, field_contract=FIELD_CONTRACT):
    """Separate the service envelope before the ordinary protected merge."""
    if not evidence_mode:
        validate_response(payload, target_fields, build_response_schema(target_fields, field_contract), field_contract)
        return payload, {}
    try:
        ContractValidator(build_evidence_response_schema(target_fields, field_contract)).validate(payload)
    except ValidationError:
        raise SchemaViolationError("Invalid internal values/evidence response envelope.") from None
    values, evidence_map = payload["values"], payload["evidence"]
    validate_response(values, target_fields, build_response_schema(target_fields, field_contract), field_contract)
    return values, evidence_map


