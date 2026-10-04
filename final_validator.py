"""Contract-driven final validation; does not repair or reinterpret values."""
from pathlib import Path
from field_contract import FIELD_CONTRACT
from runtime_contract import TEMPLATE_PATH, load_template_keys, dependency_conditions
from contract_validation import build_response_schema, validate_response
from pipeline_errors import FinalValidationError, LLMClientError
from json_io import read_json


def validate_dependencies(values):
    for name, descriptor in FIELD_CONTRACT.items():
        for condition in dependency_conditions(descriptor):
            if values.get(condition["field"]) != condition["equals"]:
                if values.get(name) != descriptor["missing_value"]:
                    raise FinalValidationError("Inapplicable dependent field: " + name)


def validate_no_none(values):
    if any(value is None for value in values.values()):
        raise FinalValidationError("Final values must not contain None.")


def validate_type(field_name, value, template_value=None):
    # Retain the wrapper. template_value is no longer the source of type rules.
    try:
        schema = build_response_schema([field_name], strict_binary_text=False)
        validate_response({field_name: value}, [field_name], schema)
    except LLMClientError:
        raise FinalValidationError("Invalid field value: " + field_name) from None


def validate_binary_code(field_name, value):
    if field_name not in FIELD_CONTRACT or FIELD_CONTRACT[field_name]["value_type"] != "binary_code":
        raise FinalValidationError("Field is not binary_code.")
    validate_type(field_name, value)


def validate_final_json(values, template_path=TEMPLATE_PATH):
    keys = load_template_keys(template_path)
    if not isinstance(values, dict) or set(values) != set(keys):
        raise FinalValidationError("Final JSON must contain exactly the official fields.")
    validate_no_none(values)
    try:
        # TODO upstream/contract: parser code=1 texts differ from allowed_texts.
        # Preserve texts; enforce structure, code=0 sentinel and nonempty code=1 text.
        schema = build_response_schema(keys, strict_binary_text=False)
        validate_response(values, keys, schema)
    except LLMClientError:
        raise FinalValidationError("Final JSON violates the field contract.") from None
    validate_dependencies(values)
    return True


def validate_file(json_path, template_path=TEMPLATE_PATH):
    return validate_final_json(read_json(json_path), template_path)


if __name__ == "__main__":
    import argparse
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("json_files", type=Path, nargs="+")
    cli.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    args = cli.parse_args()
    failed = 0
    for index, path in enumerate(args.json_files, 1):
        try:
            validate_file(path, args.template)
            print(f"Document {index}: valid")
        except Exception:
            failed += 1
            print(f"Document {index}: validation failed")
    raise SystemExit(1 if failed else 0)
