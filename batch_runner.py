"""Sequential, staged pipeline. Failed documents never publish new results."""
import hashlib
import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from deidentify import deidentify_text
from parser import extract_epicrisis_from_deidentified
from prompt_builder import build_llm_request
from llm_client import call_llm
from contract_validation import build_response_schema, validate_response, split_llm_response
from evidence_layer import validate_llm_evidence, apply_clinical_consistency, collect_service_evidence
from pipeline_errors import InvalidLLMResponseError, InvalidJSONError, UnexpectedResponseError
from privacy_guard import assert_safe_request
from merge import merge_record
from finalization import finalize_values
from final_validator import validate_final_json, validate_dependencies
from runtime_contract import TEMPLATE_PATH
from json_io import atomic_write_json, read_json

INPUT_DIR, OUTPUT_DIR = Path("input"), Path("result")
INTERMEDIATE_DIR, LOG_DIR = Path("intermediate"), Path("logs")


@dataclass
class ProcessingResult:
    file_name: str
    success: bool
    error: str | None = None
    llm_required: bool = False
    target_fields_count: int = 0
    processing_time_ms: int = 0
    stage: str = "read"
    error_type: str | None = None
    result: dict | None = None
    evidence: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_service_result(self):
        return {"success": self.success, "result": self.result if self.success else None,
                "evidence": self.evidence if self.success else {}, "meta": self.meta,
                "error": self.error, "stage": self.stage}


def document_id(path):
    # Filenames may contain patient names; logs/output names use opaque stable IDs.
    return "doc_" + hashlib.sha256(Path(path).name.encode("utf-8")).hexdigest()


def _log_result(log_file, result):
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = asdict(result)
    # Clinical values/citations belong in the service result, never the log.
    for key in ("result", "evidence", "meta"):
        entry.pop(key, None)
    entry["file"] = entry.pop("file_name")
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")


def process_single_file(input_file, output_file, intermediate_file=None,
                        log_file=None, template_path=TEMPLATE_PATH, *, service_file=None):
    started = time.perf_counter()
    stage, request = "read", {}
    try:
        paths = [Path(path).resolve() for path in (input_file, output_file, intermediate_file, log_file, service_file) if path is not None]
        if len(paths) != len(set(paths)):
            # A rejected log path must never append to the source or result.
            log_file = None
            raise ValueError("Input, output, intermediate and log paths must be distinct.")
        raw_text = Path(input_file).read_text(encoding="utf-8-sig")
        stage = "deidentify"
        redacted_text, audit = deidentify_text(raw_text)
        stage = "parser"
        record = extract_epicrisis_from_deidentified(redacted_text, template_path=template_path)
        stage = "prompt_builder"
        request = build_llm_request(redacted_text, record, include_evidence=True)
        if not isinstance(request, dict):
            raise ValueError("Prompt builder must return a request object.")
        stage = "privacy"
        # Check even skipped tasks before any optional intermediate persistence.
        assert_safe_request({**request, "llm_required": True, "user_prompt": redacted_text + "\n" + str(request.get("user_prompt", ""))})
        stage = "llm"
        payload = call_llm(request) if request["llm_required"] else {}
        stage = "llm_validation"
        targets = request["target_fields"]
        response, proposed_evidence = split_llm_response(payload, targets,
            evidence_mode=request.get("include_evidence", False) and request["llm_required"])
        validate_response(response, targets, build_response_schema(targets))
        stage = "evidence_validation"
        llm_evidence, rejected = validate_llm_evidence(response, proposed_evidence, redacted_text)
        stage = "merge"
        merged = merge_record(record, response, targets, template_path)
        stage = "clinical_consistency"
        merged, overrides, rule_sources, clarifications = apply_clinical_consistency(merged, record, redacted_text)
        stage = "finalization"
        final = finalize_values(merged, template_path)
        stage = "dependency_validation"
        validate_dependencies(final)
        stage = "final_validation"
        validate_final_json(final, template_path)
        assert_safe_request({"llm_required": True, "user_prompt": json.dumps(final, ensure_ascii=False)})
        quotations, evidence_sources = collect_service_evidence(record, final, response, llm_evidence,
            overrides, rule_sources, source_text=redacted_text)
        service = {"success": True, "result": final, "evidence": quotations,
                   "meta": {"evidence_sources": evidence_sources,
                            "rejected_evidence_fields": rejected, "clarifications": clarifications,
                            "deidentification": audit}}
        assert_safe_request({"llm_required": True, "user_prompt": json.dumps(service, ensure_ascii=False)})
        stage = "save"
        if intermediate_file is not None:
            assert_safe_request({"llm_required": True, "user_prompt": json.dumps(record, ensure_ascii=False)})
            atomic_write_json(intermediate_file, record)
        if service_file is not None:
            service_path = Path(service_file)
            service_existed = service_path.exists()
            previous_service = read_json(service_path) if service_existed else None
            atomic_write_json(service_file, service)
        try:
            atomic_write_json(output_file, final)
        except Exception:
            # Do not leave a success=true service result for a failed official save.
            if service_file is not None:
                if service_existed:
                    atomic_write_json(service_file, previous_service)
                else:
                    service_path.unlink(missing_ok=True)
            raise
        result = ProcessingResult(document_id(input_file), True, stage="complete",
                                  result=final, evidence=quotations, meta=service["meta"])
    except Exception as error:
        # The client validates before returning; classify those failures accurately.
        if stage == "llm" and isinstance(error, (InvalidLLMResponseError, InvalidJSONError, UnexpectedResponseError)):
            stage = "llm_validation"
        result = ProcessingResult(document_id(input_file), False,
                                  error="Document processing failed at stage: " + stage,
                                  stage=stage, error_type=type(error).__name__)
    request_summary = request if isinstance(request, dict) else {}
    result.llm_required = request_summary.get("llm_required") is True
    targets_summary = request_summary.get("target_fields")
    result.target_fields_count = len(targets_summary) if isinstance(targets_summary, (list, tuple)) else 0
    result.processing_time_ms = int((time.perf_counter() - started) * 1000)
    try:
        _log_result(Path(log_file) if log_file is not None else None, result)
    except Exception:
        result.success = False
        result.error = "Unable to persist the processing log."
        result.error_type = "LogWriteError"
        result.stage = "log"
    return result


def process_all_files(input_dir=INPUT_DIR, output_dir=OUTPUT_DIR,
                      intermediate_dir=INTERMEDIATE_DIR, log_dir=LOG_DIR,
                      template_path=TEMPLATE_PATH, *, service_dir=None):
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    if not input_dir.is_dir():
        raise ValueError("Input directory does not exist.")
    paths = [Path(path).resolve() for path in (input_dir, output_dir, intermediate_dir, log_dir, service_dir) if path is not None]
    if len(set(paths)) != len(paths):
        raise ValueError("Input, output, intermediate and log directories must be distinct.")
    inputs = sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in {".txt", ".md"})
    if not inputs:
        raise ValueError("No input documents found.")
    log_file = Path(log_dir) / ("batch_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".jsonl") if log_dir else None
    results = []
    for index, path in enumerate(inputs, 1):
        identifier = document_id(path)
        intermediate = Path(intermediate_dir) / (identifier + ".intermediate.json") if intermediate_dir else None
        service_file = Path(service_dir) / (identifier + ".service.json") if service_dir else None
        result = process_single_file(path, output_dir / (identifier + ".json"), intermediate, log_file,
                                     template_path, service_file=service_file)
        results.append(result)
        print(f"[{index}/{len(inputs)}] {identifier}: " + ("OK" if result.success else result.error))
    print(f"Success: {sum(result.success for result in results)}; failed: {sum(not result.success for result in results)}")
    return results


def main(argv=None):
    import argparse
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    cli.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    cli.add_argument("--intermediate-dir", type=Path, default=INTERMEDIATE_DIR)
    cli.add_argument("--log-dir", type=Path, default=LOG_DIR)
    cli.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    cli.add_argument("--service-dir", type=Path, help="Optional separate values/evidence files for the UI")
    args = cli.parse_args(argv)
    try:
        results = process_all_files(args.input_dir, args.output_dir, args.intermediate_dir,
                                    args.log_dir, args.template, service_dir=args.service_dir)
    except Exception:
        print("Batch setup failed. Check input/output directories and template.")
        return 1
    return 1 if any(not result.success for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
