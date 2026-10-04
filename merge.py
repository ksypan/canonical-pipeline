"""Guarded merge, without defaults or changes to parser metadata."""
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from field_contract import FIELD_CONTRACT
from runtime_contract import TEMPLATE_PATH, load_template_keys
from pipeline_errors import MergeError
from contract_validation import build_response_schema, validate_response


def merge_record(record, llm_response, target_fields, template_path=TEMPLATE_PATH):
    keys = load_template_keys(template_path)
    if not isinstance(record, Mapping):
        raise MergeError("record must contain values and meta.")
    values, meta = record.get("values"), record.get("meta")
    if not isinstance(values, Mapping) or not isinstance(meta, Mapping):
        raise MergeError("record must contain values and meta mappings.")
    if set(values) != set(keys) or set(meta) != set(keys):
        raise MergeError("record values/meta must have exactly the official fields.")
    schema = build_response_schema(target_fields)
    for name in keys:
        entry = meta[name]
        if not isinstance(entry, Mapping) or entry.get("status") not in {"confirmed", "needs_llm"}:
            raise MergeError("record meta contains an invalid status.")
        if entry["status"] == "confirmed" and values[name] is None:
            raise MergeError("Confirmed parser value must not be None.")
        if entry["status"] == "needs_llm" and values[name] is not None:
            raise MergeError("Unconfirmed parser value must be None.")
    for name in target_fields:
        if meta[name]["status"] != "needs_llm":
            raise MergeError("LLM must not overwrite a confirmed parser field.")
        if FIELD_CONTRACT[name]["extractor"] == "parser":
            raise MergeError("Parser-only field must not be an LLM target.")
    validate_response(llm_response, target_fields, schema)
    return {name: deepcopy(llm_response[name] if name in llm_response else values[name]) for name in keys}


def merge_from_files(intermediate_path, llm_response_path, output_path, target_fields,
                     template_path=TEMPLATE_PATH):
    from finalization import finalize_values
    from final_validator import validate_final_json
    from json_io import read_json, atomic_write_json
    from privacy_guard import assert_safe_request
    import json
    record, response = read_json(intermediate_path), read_json(llm_response_path)
    merged = merge_record(record, response, target_fields, template_path)
    final = finalize_values(merged, template_path)
    validate_final_json(final, template_path)
    assert_safe_request({"llm_required": True, "user_prompt": json.dumps(final, ensure_ascii=False)})
    atomic_write_json(output_path, final)


if __name__ == "__main__":
    import argparse
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--intermediate", type=Path, required=True)
    cli.add_argument("--llm-response", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--target-fields", required=True)
    cli.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    args = cli.parse_args()
    try:
        merge_from_files(args.intermediate, args.llm_response, args.output,
                         [name.strip() for name in args.target_fields.split(",") if name.strip()], args.template)
    except Exception:
        print("Merge/finalization/validation failed; no new output saved.")
        raise SystemExit(1)
    print("Validated output saved.")
