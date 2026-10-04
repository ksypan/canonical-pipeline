"""Explicit single-document integration test; never run by unittest."""
import argparse
import json
import sys
import time
from pathlib import Path
from deidentify import deidentify_text
from parser import extract_epicrisis_from_deidentified
from prompt_builder import build_llm_request
from llm_client import call_llm, YandexConfig, LLMClientError, LLMTimeoutError
from contract_validation import build_response_schema, validate_response, split_llm_response
from evidence_layer import validate_llm_evidence, apply_clinical_consistency, collect_service_evidence
from privacy_guard import assert_safe_request
from merge import merge_record
from finalization import finalize_values
from final_validator import validate_final_json


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--input", type=Path, default=Path(__file__).parent / "tests/fixtures/train-0003.md")
    cli.add_argument("--json-mode", action="store_true", help="Explicit JSON object mode; local schema validation still required")
    cli.add_argument("--omit-temperature", action="store_true")
    cli.add_argument("--max-tokens", type=int, default=4096)
    cli.add_argument("--full-pipeline", action="store_true")
    args = cli.parse_args(argv)
    started_at = None
    try:
        redacted_text, audit = deidentify_text(args.input.read_text(encoding="utf-8-sig"))
        record = extract_epicrisis_from_deidentified(redacted_text)
        request = build_llm_request(redacted_text, record, include_evidence=True)
        assert_safe_request({**request, "llm_required": True, "user_prompt": redacted_text + "\n" + str(request.get("user_prompt", ""))})
        config = YandexConfig.from_env() if request["llm_required"] else None
        print("target_fields:")
        print(json.dumps(request["target_fields"], ensure_ascii=False, indent=2))
        started_at = time.perf_counter()
        payload = call_llm(request, config=config,
                            response_mode="json_object" if args.json_mode else "json_schema",
                            temperature=None if args.omit_temperature else 0.0,
                            max_completion_tokens=args.max_tokens) if request["llm_required"] else {}
        print(f"LLM response time: {time.perf_counter() - started_at:.1f} s")
        response, proposed = split_llm_response(payload, request["target_fields"],
            evidence_mode=request.get("include_evidence", False) and request["llm_required"])
        validate_response(response, request["target_fields"], build_response_schema(request["target_fields"]))
        quotations, rejected = validate_llm_evidence(response, proposed, redacted_text)
        assert_safe_request({"llm_required": True, "user_prompt": json.dumps(response, ensure_ascii=False)})
        print("Validated LLM JSON:")
        print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
        if args.full_pipeline:
            merged = merge_record(record, response, request["target_fields"])
            merged, overrides, sources, clarifications = apply_clinical_consistency(merged, record, redacted_text)
            final = finalize_values(merged)
            validate_final_json(final)
            assert_safe_request({"llm_required": True, "user_prompt": json.dumps(final, ensure_ascii=False)})
            print("Final validation: OK")
            print(json.dumps(final, ensure_ascii=False, indent=2, allow_nan=False))
            quotes, origins = collect_service_evidence(record, final, response, quotations, overrides,
                                                       sources, source_text=redacted_text)
            service = {"evidence": quotes, "meta": {"evidence_sources": origins,
                       "rejected_evidence_fields": rejected, "clarifications": clarifications}}
            assert_safe_request({"llm_required": True, "user_prompt": json.dumps(service, ensure_ascii=False)})
            print("Service evidence (separate from official JSON):")
            print(json.dumps(service, ensure_ascii=False, indent=2))
    except LLMTimeoutError:
        if started_at is not None:
            print(f"LLM response time before timeout: {time.perf_counter() - started_at:.1f} s", file=sys.stderr)
        print("Yandex AI Studio timeout; no result generated.", file=sys.stderr)
        return 1
    except LLMClientError as error:
        # This hierarchy has safe messages, including names of missing env variables.
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Input, parsing, merge or final validation failed; check the contract and input.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
