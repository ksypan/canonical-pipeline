from field_contract import NOT_SPECIFIED
import contextlib
import hashlib
import io
import json
import shutil
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from field_contract import FIELD_CONTRACT
from runtime_contract import TEMPLATE_PATH, load_template_keys
from parser import extract_epicrisis_from_deidentified
from deidentify import deidentify_text
from prompt_builder import build_llm_request
from merge import merge_record, merge_from_files
from finalization import finalize_values
from final_validator import validate_final_json, validate_file
from pipeline_errors import MergeError, FinalizationError, FinalValidationError, MissingFieldsError, ExtraFieldsError, SchemaViolationError, LLMTimeoutError
from json_io import atomic_write_json, read_json
import batch_runner
import manual_llm_test
from llm_client import YandexConfig, YandexLLMClient

ROOT = Path(__file__).resolve().parents[1]


def empty_record():
    return {"values": {name: None for name in FIELD_CONTRACT},
            "meta": {name: {"status": "needs_llm", "source": None, "evidence": None} for name in FIELD_CONTRACT}}


def confirmed_record():
    return {"values": {name: deepcopy(desc["missing_value"]) for name, desc in FIELD_CONTRACT.items()},
            "meta": {name: {"status": "confirmed", "source": "parser", "evidence": "synthetic fixture"} for name in FIELD_CONTRACT}}


def defaults():
    return finalize_values(empty_record()["values"])


def missing_response(request):
    values = {name: deepcopy(FIELD_CONTRACT[name]["missing_value"]) for name in request["target_fields"]}
    return {"values": values, "evidence": {name: None for name in values}} if request.get("include_evidence") else values


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.record = empty_record()
        self.record["values"]["bp"] = "123/79"
        self.record["meta"]["bp"] = {"status": "confirmed", "source": "parser", "evidence": "АД 123/79"}

    def test_merge_rejects_confirmed_target(self):
        with self.assertRaises(MergeError):
            merge_record(self.record, {"bp": "160/90"}, ["bp"])

    def test_merge_preserves_confirmed(self):
        result = merge_record(self.record, {"ca_fact": "Y"}, ["ca_fact"])
        self.assertEqual(result["bp"], "123/79")
        self.assertEqual(result["ca_fact"], "Y")
        self.assertIsNone(result["bpm"])
        self.assertEqual(tuple(result), load_template_keys())

    def test_merge_missing_llm_target(self):
        with self.assertRaises(MissingFieldsError):
            merge_record(self.record, {}, ["ca_fact"])

    def test_merge_extra_llm_target(self):
        with self.assertRaises(ExtraFieldsError):
            merge_record(self.record, {"ca_fact": "Y", "ca_lad": 1}, ["ca_fact"])

    def test_merge_parser_only_target(self):
        with self.assertRaises(MergeError):
            merge_record(self.record, {"bpm": 70}, ["bpm"])

    def test_merge_validates_values_before_overlay(self):
        with self.assertRaises(SchemaViolationError):
            merge_record(self.record, {"ca_fact": "bad"}, ["ca_fact"])

    def test_merge_no_input_mutation_or_aliases(self):
        response = {"aspirin": [{"name": "АСК", "prescription": NOT_SPECIFIED}]}
        record_before, response_before = deepcopy(self.record), deepcopy(response)
        merged = merge_record(self.record, response, ["aspirin"])
        merged["aspirin"][0]["name"] = "changed"
        self.assertEqual(self.record, record_before)
        self.assertEqual(response, response_before)

    def test_merge_rejects_record_extra_or_missing(self):
        for block in ["values", "meta"]:
            for add in [True, False]:
                record = deepcopy(self.record)
                if add:
                    record[block]["invented"] = None
                else:
                    del record[block]["ca_fact"]
                with self.subTest(block=block, add=add), self.assertRaises(MergeError):
                    merge_record(record, {}, [])

    def test_merge_invalid_status(self):
        self.record["meta"]["ca_fact"]["status"] = "maybe"
        with self.assertRaises(MergeError):
            merge_record(self.record, {}, [])

    def test_merge_confirmed_none_invalid(self):
        self.record["meta"]["ca_fact"]["status"] = "confirmed"
        with self.assertRaises(MergeError):
            merge_record(self.record, {}, [])

    def test_merge_unconfirmed_non_none_invalid(self):
        self.record["values"]["ca_fact"] = "Y"
        with self.assertRaises(MergeError):
            merge_record(self.record, {}, [])


class FinalizationTests(unittest.TestCase):
    def test_ordinary_default(self):
        self.assertEqual(defaults()["bpm"], FIELD_CONTRACT["bpm"]["missing_value"])

    def test_binary_default(self):
        self.assertEqual(defaults()["art_hyper"], {"code": 0, "text": NOT_SPECIFIED})

    def test_medication_default(self):
        self.assertEqual(defaults()["aspirin"], 0)

    def test_ca_fact_default(self):
        self.assertEqual(defaults()["ca_fact"], "N")

    def test_ca_fact_n_dependency(self):
        self.assert_dependency("N")

    def test_ca_fact_r_dependency(self):
        self.assert_dependency("R")

    def test_ca_fact_unresolved_dependency(self):
        self.assert_dependency(None)

    def assert_dependency(self, fact):
        values = empty_record()["values"]
        values.update(ca_fact=fact, ca_lad=2, rca=1, ca_date='25.03.2022')
        final = finalize_values(values)
        for field in ["ca_lad", "rca", "ca_date"]:
            self.assertEqual(final[field], FIELD_CONTRACT[field]["missing_value"])
        self.assertTrue(validate_final_json(final))
        self.assertEqual(values["ca_lad"], 2)

    def test_ca_fact_y_dependency(self):
        values = empty_record()["values"]
        values.update(ca_fact="Y", ca_lad=2, rca=0, ca_date='25.03.2022')
        final = finalize_values(values)
        self.assertEqual((final["ca_lad"], final["rca"], final["ca_date"]), (2, 0, '25.03.2022'))
        self.assertTrue(validate_final_json(final))

    def test_conflicting_llm_response_normalized_by_dependency(self):
        record = empty_record()
        record["values"]["ca_fact"] = "R"
        record["meta"]["ca_fact"]["status"] = "confirmed"
        merged = merge_record(record, {"ca_lad": 2, "rca": 1}, ["ca_lad", "rca"])
        self.assertEqual(merged["ca_lad"], 2)
        self.assertEqual(finalize_values(merged)["ca_lad"], NOT_SPECIFIED)

    def test_finalization_does_not_change_meta_or_contract(self):
        record = empty_record()
        before, contract = deepcopy(record), deepcopy(FIELD_CONTRACT)
        result = finalize_values(record["values"])
        result["art_hyper"]["text"] = "changed"
        self.assertEqual(record, before)
        self.assertEqual(FIELD_CONTRACT, contract)

    def test_finalization_requires_exact_keys(self):
        values = empty_record()["values"]
        del values["aspirin"]
        with self.assertRaises(FinalizationError):
            finalize_values(values)


class ValidatorTests(unittest.TestCase):
    def invalid(self, **updates):
        result = defaults()
        result.update(updates)
        with self.assertRaises(FinalValidationError):
            validate_final_json(result)

    def test_exact_50_and_official_order(self):
        result = defaults()
        self.assertEqual(len(result), 50)
        self.assertEqual(tuple(result), tuple(json.loads(TEMPLATE_PATH.read_text(encoding="utf-8-sig"))))
        self.assertTrue(validate_final_json(result))

    def test_missing_final_field(self):
        result = defaults()
        del result["aspirin"]
        with self.assertRaises(FinalValidationError):
            validate_final_json(result)

    def test_extra_final_metadata(self):
        for field in ["meta", "evidence", "target_fields", "deidentification"]:
            with self.subTest(field=field):
                self.invalid(**{field: {}})

    def test_none_rejected(self):
        self.invalid(bpm=None)

    def test_bool_not_number(self):
        self.invalid(bpm=True)

    def test_nonfinite_rejected(self):
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.subTest(value=value):
                self.invalid(echo_lvd_2=value)

    def test_impossible_date(self):
        self.invalid(admission_date='31.02.2025')

    def test_date_format_from_contract(self):
        self.invalid(admission_date="2025-01-01")
        self.invalid(admission_date="1/1/2025")

    def test_leap_date_accepted(self):
        result = defaults()
        result["admission_date"] = '29.02.2024'
        self.assertTrue(validate_final_json(result))

    def test_medication_empty_list(self):
        self.invalid(aspirin=[])

    def test_medication_extra_key(self):
        self.invalid(aspirin=[{"name": "АСК", "prescription": "100 мг", "extra": 1}])

    def test_medication_wrong_shapes(self):
        for value in [1, True, NOT_SPECIFIED, {"name": "АСК"}, [{"name": "АСК"}], [{"name": "", "prescription": "100 мг"}], [{"name": "АСК", "prescription": " "}]]:
            with self.subTest(value=value):
                self.invalid(aspirin=value)

    def test_medication_valid_missing_prescription(self):
        result = defaults()
        result["aspirin"] = [{"name": "АСК", "prescription": NOT_SPECIFIED}]
        self.assertTrue(validate_final_json(result))

    def test_binary_extra_key(self):
        self.invalid(art_hyper={"code": 1, "text": "ГБ", "extra": 1})

    def test_binary_bool_float_wrong_code(self):
        for value in [True, 1.0, 2, "1"]:
            with self.subTest(value=value):
                self.invalid(art_hyper={"code": value, "text": "ГБ"})

    def test_binary_empty_text(self):
        self.invalid(art_hyper={"code": 1, "text": " "})

    def test_binary_zero_exact_missing(self):
        self.invalid(art_hyper={"code": 0, "text": "нет"})

    def test_wrong_missing_markers(self):
        for field in ["ca_fact", "aspirin", "art_hyper"]:
            with self.subTest(field=field):
                self.invalid(**{field: NOT_SPECIFIED})

    def test_echo_lvd_2_numeric_accepted(self):
        result = defaults()
        result["echo_lvd_2"] = 35.2
        self.assertTrue(validate_final_json(result))

    def test_echo_lvd_2_string_rejected(self):
        self.invalid(echo_lvd_2="35.2")

    def test_enum_validation(self):
        for field, value in [("ca_fact", "yes"), ("killip", 5), ("killip", True), ("card_trop", "high"), ("ca_lad", 3), ("rca", True)]:
            with self.subTest(field=field):
                self.invalid(ca_fact="Y", **{field: value}) if field != "ca_fact" else self.invalid(ca_fact=value)

    def test_string_pattern_validation(self):
        self.invalid(bp="120 over 80")
        self.invalid(diagnosis_icd="I21.invalid")

    def test_dependencies_rechecked(self):
        self.invalid(ca_fact="N", ca_lad=1)
        self.invalid(ca_fact="R", rca=0)
        self.invalid(ca_fact="N", ca_date='01.01.2020')

    def test_preserve_parser_binary_text_pending_clarification(self):
        result = defaults()
        result["echo_zone"] = {"code": 1, "text": "Гипокинез"}
        self.assertTrue(validate_final_json(result))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ".test-run" / uuid.uuid4().hex
        self.folder.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_atomic_strict_json(self):
        path = self.folder / "nested/result.json"
        atomic_write_json(path, defaults())
        self.assertTrue(validate_file(path))
        self.assertEqual(tuple(read_json(path)), tuple(FIELD_CONTRACT))

    def test_nonfinite_never_published(self):
        path = self.folder / "result.json"
        with self.assertRaises(ValueError):
            atomic_write_json(path, {"x": float("nan")})
        self.assertFalse(path.exists())

    def test_failed_atomic_replace_preserves_previous(self):
        path = self.folder / "result.json"
        atomic_write_json(path, {"previous": 1})
        with patch("json_io.os.replace", side_effect=OSError("simulated")), self.assertRaises(OSError):
            atomic_write_json(path, {"new": 1})
        self.assertEqual(read_json(path), {"previous": 1})
        self.assertEqual(list(self.folder.glob(".pending-*")), [])

    def test_file_duplicate_keys_and_nan_rejected(self):
        path = self.folder / "bad.json"
        for text in ['{"a":1,"a":2}', '{"a":NaN}']:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_json(path)

    def test_offline_merge_finalizes_and_validates(self):
        record, response, output = [self.folder / name for name in ["record.json", "response.json", "final.json"]]
        atomic_write_json(record, empty_record())
        atomic_write_json(response, {"ca_fact": "R"})
        merge_from_files(record, response, output, ["ca_fact"])
        self.assertTrue(validate_file(output))
        self.assertEqual(read_json(output)["aspirin"], 0)

    def test_mocked_llm_end_to_end_success(self):
        source = ROOT / "tests/fixtures/train-0003.md"
        output, intermediate, log = [self.folder / name for name in ["result.json", "intermediate.json", "log/log.jsonl"]]
        with patch("batch_runner.call_llm", side_effect=missing_response) as llm:
            result = batch_runner.process_single_file(source, output, intermediate, log)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.stage, "complete")
        self.assertTrue(validate_file(output))
        final = read_json(output)
        self.assertEqual(final["bp"], "123/79")
        self.assertEqual(final["dm"]["code"], 1)
        self.assertEqual(final["ca_date"], '25.03.2022')
        self.assertEqual(tuple(final), tuple(FIELD_CONTRACT))
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(read_json(intermediate)["meta"]["ca_lad"]["status"], "needs_llm")

    def test_full_pipeline_with_mocked_http_transport(self):
        source = ROOT / "tests/fixtures/train-0003.md"
        text, audit = deidentify_text(source.read_text(encoding="utf-8"))
        record = extract_epicrisis_from_deidentified(text)
        record["deidentification"] = audit
        request = build_llm_request(text, record, include_evidence=True)
        envelope = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                    "content": json.dumps(missing_response(request), ensure_ascii=False)}}]}
        transport = Mock(return_value=io.BytesIO(json.dumps(envelope, ensure_ascii=False).encode("utf-8")))
        config = YandexConfig.from_env({"YANDEX_API_KEY": "mock-placeholder-not-a-real-key",
                                        "YANDEX_MODEL": "mock-model", "YANDEX_FOLDER_ID": "mock-folder"})
        client = YandexLLMClient(config, transport=transport)
        output = self.folder / "full.json"
        with patch("batch_runner.call_llm", side_effect=client.call):
            result = batch_runner.process_single_file(source, output)
        self.assertTrue(result.success, result.error)
        self.assertTrue(validate_file(output))
        self.assertEqual(transport.call_count, 1)

    def test_invalid_llm_does_not_write_result(self):
        output = self.folder / "result.json"
        with patch("batch_runner.call_llm", return_value={"extra": 1}):
            result = batch_runner.process_single_file(ROOT / "tests/fixtures/train-0003.md", output)
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "llm_validation")
        self.assertFalse(output.exists())

    def test_no_llm_still_valid_final(self):
        request = {"llm_required": False, "target_fields": [], "confirmed_values": {}, "system_prompt": None, "user_prompt": None}
        source = self.folder / "input.md"
        source.write_text("Синтетический документ без идентификаторов.", encoding="utf-8")
        output = self.folder / "result.json"
        with patch("batch_runner.extract_epicrisis_from_deidentified", return_value=confirmed_record()), \
             patch("batch_runner.build_llm_request", return_value=request), patch("batch_runner.call_llm") as llm:
            result = batch_runner.process_single_file(source, output)
        self.assertTrue(result.success, result.error)
        self.assertTrue(validate_file(output))
        llm.assert_not_called()

    def test_train_2_phi_fails_closed_without_call(self):
        output = self.folder / "result.json"
        with patch("batch_runner.call_llm") as llm:
            result = batch_runner.process_single_file(ROOT / "tests/fixtures/train-0002.md", output)
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "privacy")
        llm.assert_not_called()
        self.assertFalse(output.exists())

    def test_pipeline_errors_report_actual_stage(self):
        source = ROOT / "tests/fixtures/train-0003.md"
        functions = [("deidentify_text", "deidentify"), ("extract_epicrisis_from_deidentified", "parser"),
                     ("build_llm_request", "prompt_builder"), ("call_llm", "llm"),
                     ("validate_response", "llm_validation"), ("merge_record", "merge"),
                     ("finalize_values", "finalization"), ("validate_dependencies", "dependency_validation"),
                     ("validate_final_json", "final_validation"), ("atomic_write_json", "save")]
        for function, stage in functions:
            output = self.folder / (stage + ".json")
            with self.subTest(stage=stage), patch("batch_runner.call_llm", side_effect=missing_response), \
                 patch("batch_runner." + function, side_effect=ValueError("sensitive raw input")):
                result = batch_runner.process_single_file(source, output)
            self.assertFalse(result.success)
            self.assertEqual(result.stage, stage)
            self.assertNotIn("sensitive", result.error)
            self.assertFalse(output.exists())

    def test_client_validation_error_classified(self):
        with patch("batch_runner.call_llm", side_effect=SchemaViolationError("safe")):
            result = batch_runner.process_single_file(ROOT / "tests/fixtures/train-0003.md", self.folder / "out.json")
        self.assertEqual(result.stage, "llm_validation")

    def test_malformed_upstream_request_returns_failure_without_crashing(self):
        for request in [None, {"llm_required": True, "target_fields": None}]:
            with self.subTest(request=request), patch("batch_runner.build_llm_request", return_value=request), \
                 patch("batch_runner.call_llm", side_effect=ValueError("invalid request")):
                result = batch_runner.process_single_file(ROOT / "tests/fixtures/train-0003.md", self.folder / "out.json")
            self.assertFalse(result.success)
            self.assertEqual(result.target_fields_count, 0)
            self.assertFalse((self.folder / "out.json").exists())

    def test_safe_logs_and_filename(self):
        source = self.folder / "Иванов Иван Иванович.md"
        source.write_text("Без идентификаторов.", encoding="utf-8")
        log = self.folder / "log/result.jsonl"
        secret = "SIMULATED-SECRET-NOT-A-REAL-KEY"
        with patch("batch_runner.deidentify_text", side_effect=RuntimeError(secret + " raw patient data")):
            result = batch_runner.process_single_file(source, self.folder / "out.json", log_file=log)
        text = log.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertNotIn("Иванов", text)
        self.assertNotIn("raw patient data", text)
        entry = json.loads(text)
        self.assertEqual(entry["stage"], "deidentify")
        self.assertEqual(entry["error_type"], "RuntimeError")
        self.assertEqual(entry["file"], result.file_name)

    def test_batch_continues_after_failed_document(self):
        inputs = self.folder / "input"
        inputs.mkdir()
        shutil.copyfile(ROOT / "tests/fixtures/train-0001.md", inputs / "a.md")
        shutil.copyfile(ROOT / "tests/fixtures/train-0003.md", inputs / "b.md")
        calls = 0
        def model(request):
            nonlocal calls
            calls += 1
            return {} if calls == 1 else missing_response(request)
        with patch("batch_runner.call_llm", side_effect=model), contextlib.redirect_stdout(io.StringIO()):
            results = batch_runner.process_all_files(inputs, self.folder / "out", None, self.folder / "logs")
        self.assertEqual([result.success for result in results], [False, True])
        self.assertEqual(len(list((self.folder / "out").glob("*.json"))), 1)

    def test_batch_nonzero_exit_for_failure(self):
        results = [batch_runner.ProcessingResult("opaque", False, stage="llm")]
        with patch("batch_runner.process_all_files", return_value=results):
            self.assertEqual(batch_runner.main([]), 1)

    def test_batch_same_stem_different_extensions_unique_ids(self):
        self.assertNotEqual(batch_runner.document_id(Path("same.md")), batch_runner.document_id(Path("same.txt")))
        self.assertEqual(batch_runner.document_id(Path("same.md")), batch_runner.document_id(Path("same.md")))

    def test_source_output_alias_blocked(self):
        source = self.folder / "input.md"
        source.write_text("original", encoding="utf-8")
        result = batch_runner.process_single_file(source, source)
        self.assertFalse(result.success)
        self.assertEqual(source.read_text(), "original")

    def test_source_log_alias_does_not_modify_input(self):
        source = self.folder / "input.md"
        source.write_text("original", encoding="utf-8")
        result = batch_runner.process_single_file(source, self.folder / "out.json", log_file=source)
        self.assertFalse(result.success)
        self.assertEqual(source.read_text(), "original")

    def test_log_write_failure_is_reported(self):
        with patch("batch_runner.call_llm", side_effect=missing_response), \
             patch("batch_runner._log_result", side_effect=OSError("sensitive message")):
            result = batch_runner.process_single_file(ROOT / "tests/fixtures/train-0003.md", self.folder / "out.json", log_file=self.folder / "log")
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "log")
        self.assertNotIn("sensitive", result.error)

    def test_manual_phi_blocked_before_call_or_print(self):
        with patch("manual_llm_test.call_llm") as llm, \
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            exit_code = manual_llm_test.main(["--input", str(ROOT / "tests/fixtures/train-0002.md")])
        self.assertEqual(exit_code, 1)
        llm.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("Ф.И.О.", stderr.getvalue())

    def test_manual_full_pipeline_mocked(self):
        with patch("manual_llm_test.YandexConfig.from_env", return_value=None), \
             patch("manual_llm_test.call_llm", side_effect=lambda request, **kwargs: missing_response(request)), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = manual_llm_test.main(["--full-pipeline"])
        self.assertEqual(exit_code, 0)
        self.assertIn("LLM response time:", stdout.getvalue())
        self.assertIn("Validated LLM JSON:", stdout.getvalue())
        self.assertIn("Final validation: OK", stdout.getvalue())

    def test_manual_timeout_reports_elapsed_time(self):
        with patch("manual_llm_test.YandexConfig.from_env", return_value=None), \
             patch("manual_llm_test.call_llm", side_effect=LLMTimeoutError("safe timeout")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            exit_code = manual_llm_test.main([])
        self.assertEqual(exit_code, 1)
        self.assertIn("LLM response time before timeout:", stderr.getvalue())


class SourceContractTests(unittest.TestCase):
    def test_merged_sources_and_fixtures_unchanged(self):
        manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
        for name, expected in manifest.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected)

    def test_template_contract_order_and_defaults(self):
        self.assertEqual(len(load_template_keys()), 50)

    def test_train_confirmed_preserved_after_merge_and_finalize(self):
        for name in ["train-0001.md", "train-0003.md"]:
            with self.subTest(name=name):
                redacted, audit = deidentify_text((ROOT / "tests/fixtures" / name).read_text(encoding="utf-8"))
                record = extract_epicrisis_from_deidentified(redacted)
                record["deidentification"] = audit
                before = deepcopy(record)
                request = build_llm_request(redacted, record)
                final = finalize_values(merge_record(record, missing_response(request), request["target_fields"]))
                self.assertTrue(validate_final_json(final))
                self.assertEqual(record, before)
                for field, entry in record["meta"].items():
                    if entry["status"] == "confirmed":
                        self.assertEqual(final[field], record["values"][field], field)
