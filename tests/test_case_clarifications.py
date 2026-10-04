import io
import json
import re
import shutil
import socket
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import batch_runner
from clinical_rules import ICD_MI_LOCALISATION, stenosis_category
from contract_validation import build_response_schema, build_evidence_response_schema, split_llm_response
from evidence_layer import apply_clinical_consistency, validate_llm_evidence, collect_service_evidence
from field_contract import FIELD_CONTRACT, NOT_SPECIFIED, DATE_FORMAT
from finalization import finalize_values
from final_validator import validate_final_json, validate_type
from llm_client import YandexConfig, YandexLLMClient
from parser import create_empty_template, extract_epicrisis_from_deidentified
from pipeline_errors import FinalValidationError, SchemaViolationError, FinalizationError
from prompt_builder import build_llm_request
from run_tests_offline import deny_network

ROOT = Path(__file__).resolve().parents[1]


def resolve(text, **updates):
    record = extract_epicrisis_from_deidentified(text)
    values = {key: deepcopy(record["values"][key] if record["values"][key] is not None
                            else desc["missing_value"]) for key, desc in FIELD_CONTRACT.items()}
    values.update(updates)
    result, quotes, sources, clarifications = apply_clinical_consistency(values, record, text)
    return finalize_values(result), record, quotes, sources, clarifications


class DateAndMissingTests(unittest.TestCase):
    def test_dot_input_normalized_to_dots(self):
        self.assertEqual(extract_epicrisis_from_deidentified("Поступил 01.01.2020")["values"]["admission_date"], "01.01.2020")

    def test_slash_input_normalized_to_dots(self):
        self.assertEqual(extract_epicrisis_from_deidentified("Поступил 01/01/2020")["values"]["admission_date"], "01.01.2020")

    def test_all_date_fields_use_dots(self):
        self.assertEqual(DATE_FORMAT, "DD.MM.YYYY")
        for field, desc in FIELD_CONTRACT.items():
            if desc["value_type"] == "date":
                with self.subTest(field=field):
                    validate_type(field, "23.03.2022")
                    with self.assertRaises(FinalValidationError):
                        validate_type(field, "23/03/2022")

    def test_impossible_calendar_date_rejected(self):
        with self.assertRaises(FinalValidationError):
            validate_type("admission_date", "31.02.2025")
        self.assertIsNone(extract_epicrisis_from_deidentified("Поступил 31.02.2025")["values"]["admission_date"])

    def test_marker_exact_and_template_consistent(self):
        self.assertEqual(NOT_SPECIFIED, "Не указано")
        template = json.loads((ROOT / "participant-output-template-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(template, {k: v["missing_value"] for k, v in FIELD_CONTRACT.items()})
        self.assertEqual(template["art_hyper"], {"code": 0, "text": "Не указано"})
        self.assertEqual(template["aspirin"], 0)

    def test_old_marker_rejected(self):
        with self.assertRaises(FinalValidationError):
            validate_type("admission_date", "не указано")
        with self.assertRaises(FinalValidationError):
            validate_type("rg_pc", {"code": 0, "text": "не указано"})
        with self.assertRaises(FinalValidationError):
            validate_type("aspirin", [{"name": "Аспирин", "prescription": "не указано"}])

    def test_intermediate_unknown_stays_none(self):
        record = extract_epicrisis_from_deidentified("Диагноз:\nДанных недостаточно")
        self.assertIsNone(record["values"]["mi_localisation"])
        self.assertEqual(record["meta"]["mi_localisation"]["status"], "needs_llm")

    def test_final_defaults_use_contract_without_mutation(self):
        record = create_empty_template()
        before = deepcopy(record)
        final = finalize_values(record["values"])
        self.assertEqual(final, {k: v["missing_value"] for k, v in FIELD_CONTRACT.items()})
        self.assertTrue(validate_final_json(final))
        self.assertEqual(record, before)


class LocalisationTests(unittest.TestCase):
    def test_declarative_icd_mapping_is_narrow(self):
        self.assertEqual(ICD_MI_LOCALISATION, {"I21.0": "передней стенки", "I21.1": "нижней стенки"})
        self.assertTrue(set(ICD_MI_LOCALISATION.values()).issubset(FIELD_CONTRACT["mi_localisation"]["allowed_texts"]))

    def test_explicit_localisation_beats_icd(self):
        text = "Диагноз:\nОстрый инфаркт миокарда нижней стенки.\nКод МКБ-10 I21.0"
        result, _, quotes, sources, _ = resolve(text)
        self.assertEqual(result["mi_localisation"], {"code": 1, "text": "нижней стенки"})
        self.assertEqual(sources["mi_localisation"], "diagnosis_text")
        self.assertIn(quotes["mi_localisation"], text)

    def test_icd_fallback_for_each_approved_code(self):
        for code, category in ICD_MI_LOCALISATION.items():
            with self.subTest(code=code):
                text = "Диагноз:\nОстрый инфаркт миокарда.\nКод МКБ-10 " + code
                result, record, quotes, sources, _ = resolve(text)
                self.assertEqual(result["mi_localisation"], {"code": 1, "text": category})
                self.assertEqual(sources["mi_localisation"], "icd_fallback")
                self.assertEqual(quotes["mi_localisation"], record["meta"]["diagnosis_icd"]["evidence"])
        # Mentioning the left ventricle alone does not specify an MI wall.
        final, _, _, _, _ = resolve("Диагноз:\nИнфаркт миокарда левого желудочка\nКод МКБ-10 I21.0")
        self.assertEqual(final["mi_localisation"], {"code": 1, "text": "передней стенки"})

    def test_ambiguous_icd_never_guesses(self):
        for code in ("I21.2", "I21.3", "I21.4", "I21.9", "I21.10"):
            with self.subTest(code=code):
                final, _, quotes, _, clarifications = resolve("Диагноз:\nКод МКБ-10 " + code,
                    mi_localisation={"code": 1, "text": "боковой стенки"})
                self.assertEqual(final["mi_localisation"], FIELD_CONTRACT["mi_localisation"]["missing_value"])
                self.assertNotIn("mi_localisation", quotes)
                self.assertTrue(any("CONTRACT_CLARIFICATION" in item for item in clarifications))

    def test_ecg_and_coronary_anatomy_not_used(self):
        text = "Диагноз:\nИнфаркт миокарда\nЭКГ:\nИнфаркт миокарда передней стенки\nКАГ:\nВыполнена КАГ\nПМЖВ: окклюзия"
        final, _, _, _, _ = resolve(text, mi_localisation={"code": 1, "text": "передней стенки"})
        self.assertEqual(final["mi_localisation"], FIELD_CONTRACT["mi_localisation"]["missing_value"])

    def test_conflicting_explicit_sites_not_resolved_by_icd(self):
        text = "Диагноз:\nИнфаркт миокарда передней стенки и нижней стенки\nКод МКБ-10 I21.0"
        final, _, _, _, flags = resolve(text)
        self.assertEqual(final["mi_localisation"], FIELD_CONTRACT["mi_localisation"]["missing_value"])
        self.assertTrue(flags)

    def test_negated_or_uncertain_mi_does_not_use_icd(self):
        for phrase in ("Инфаркт миокарда не подтверждён", "Подозрение на инфаркт миокарда передней стенки"):
            with self.subTest(phrase=phrase):
                final, _, _, _, _ = resolve("Диагноз:\n" + phrase + "\nКод МКБ-10 I21.0")
                self.assertEqual(final["mi_localisation"], FIELD_CONTRACT["mi_localisation"]["missing_value"])


class CoronaryAndXrayTests(unittest.TestCase):
    def test_lad_no_significant_stenosis_corrects_llm(self):
        text = "КАГ:\nВыполнена КАГ\nПМЖВ: гемодинамически значимого стеноза нет"
        final, _, quotes, _, _ = resolve(text, ca_lad=2)
        self.assertEqual(final["ca_lad"], 0)
        self.assertIn(quotes["ca_lad"], text)

    def test_rca_no_significant_stenosis_corrects_llm(self):
        final, _, _, _, _ = resolve("КАГ:\nВыполнена КАГ\nПКА: гемодинамически значимого стеноза нет", rca=2)
        self.assertEqual(final["rca"], 0)

    def test_vessel_scopes_are_separate(self):
        final, _, _, _, _ = resolve("КАГ:\nВыполнена КАГ\nПМЖВ: гемодинамически значимого стеноза нет; ПКА: стеноз 95%")
        self.assertEqual((final["ca_lad"], final["rca"]), (0, 2))

    def test_95_percent_is_category_two(self):
        final, _, _, _, _ = resolve("КАГ:\nВыполнена КАГ\nLAD: стеноз 95%")
        self.assertEqual(final["ca_lad"], 2)

    def test_occlusion_is_category_two(self):
        final, _, _, _, _ = resolve("КАГ:\nВыполнена КАГ\nRCA: окклюзия")
        self.assertEqual(final["rca"], 2)

    def test_thresholds_and_no_rounding(self):
        for percent, category in ((49.5, 0), (50, 1), (89, 1), (89.5, None), (90, 2), (100, 2), (101, None)):
            with self.subTest(percent=percent):
                self.assertEqual(stenosis_category(percent), category)
        final, _, _, _, flags = resolve("КАГ:\nВыполнена КАГ\nLAD: стеноз 89,5%", ca_lad=2)
        self.assertEqual(final["ca_lad"], NOT_SPECIFIED)
        self.assertTrue(flags)

    def test_multiple_lesions_and_ranges_remain_unknown(self):
        for finding in ("стеноз 40% и 95%", "стеноз 50–89%", "стеноз >90%"):
            with self.subTest(finding=finding):
                final, _, _, _, flags = resolve("КАГ:\nВыполнена КАГ\nLAD: " + finding, ca_lad=2)
                self.assertEqual(final["ca_lad"], NOT_SPECIFIED)
                self.assertTrue(flags)

    def test_conflicting_coronary_phrases_do_not_guess(self):
        final, _, _, _, flags = resolve("КАГ:\nВыполнена КАГ\nLAD: гемодинамически значимого стеноза нет, стеноз 95%")
        self.assertEqual(final["ca_lad"], NOT_SPECIFIED)
        self.assertTrue(flags)

    def test_coronary_requires_completed_ca(self):
        final, _, quotes, _, _ = resolve("КАГ:\nКАГ планируется\nПМЖВ: стеноз 95%")
        self.assertEqual(final["ca_lad"], NOT_SPECIFIED)
        self.assertNotIn("ca_lad", quotes)

    def test_explicit_negative_xray_is_zero(self):
        for phrase in ("Признаков отёка лёгких не выявлено", "Венозного застоя нет", "Данных за венозный застой в лёгких не получено"):
            with self.subTest(phrase=phrase):
                text = "Рентгенография ОГК:\n" + phrase
                final, record, quotes, _, _ = resolve(text)
                self.assertEqual(final["rg_pc"], {"code": 0, "text": NOT_SPECIFIED})
                self.assertEqual(quotes["rg_pc"], record["meta"]["rg_pc"]["evidence"])
                self.assertIn(quotes["rg_pc"], text)


class EvidenceTests(unittest.TestCase):
    def test_llm_evidence_is_short_verbatim_and_supports_value(self):
        quote = "Тропонин отрицательный."
        accepted, rejected = validate_llm_evidence({"card_trop": "отрицательный"}, {"card_trop": quote}, "Анализы крови:\n" + quote)
        self.assertEqual(accepted, {"card_trop": quote})
        self.assertEqual(rejected, [])

    def test_hallucinated_evidence_omitted(self):
        accepted, rejected = validate_llm_evidence({"card_trop": "положительный"}, {"card_trop": "Тропонин положительный."}, "Анализы крови:\nТропонин отрицательный.")
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, ["card_trop"])

    def test_substring_alone_is_not_proof(self):
        quote = "Тропонин отрицательный."
        accepted, rejected = validate_llm_evidence({"card_trop": "положительный"}, {"card_trop": quote}, "Анализы крови:\n" + quote)
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, ["card_trop"])

    def test_negated_enum_mention_not_proof(self):
        quote = "Тропонин не положительный."
        accepted, _ = validate_llm_evidence({"card_trop": "положительный"}, {"card_trop": quote}, "Анализы крови:\n" + quote)
        self.assertEqual(accepted, {})

    def test_unrelated_number_not_proof(self):
        quote = "ЧДД 20"
        accepted, _ = validate_llm_evidence({"bmi": 20}, {"bmi": quote}, "Первичный осмотр:\n" + quote)
        self.assertEqual(accepted, {})

    def test_dotted_clinical_sentence_keeps_section(self):
        text = "Диагноз:\nОстрый инфаркт миокарда нижней стенки."
        final, _, quotes, _, _ = resolve(text)
        self.assertEqual(final["mi_localisation"], {"code": 1, "text": "нижней стенки"})
        self.assertIn(quotes["mi_localisation"], text)

    def test_evidence_from_wrong_section_omitted(self):
        quote = "Тропонин положительный."
        accepted, _ = validate_llm_evidence({"card_trop": "положительный"}, {"card_trop": quote}, "Анамнез:\n" + quote)
        self.assertEqual(accepted, {})

    def test_missing_has_no_invented_evidence(self):
        self.assertEqual(validate_llm_evidence({"ca_lad": NOT_SPECIFIED}, {"ca_lad": None}, "КАГ:\nНет сведений"), ({}, []))

    def test_service_schema_values_separate_from_evidence(self):
        schema = build_evidence_response_schema(["card_trop"])
        self.assertEqual(set(schema["properties"]), {"values", "evidence"})
        values, quotes = split_llm_response({"values": {"card_trop": "положительный"}, "evidence": {"card_trop": None}}, ["card_trop"], evidence_mode=True)
        self.assertEqual(values, {"card_trop": "положительный"})
        self.assertEqual(quotes, {"card_trop": None})
        with self.assertRaises(SchemaViolationError):
            split_llm_response({"values": values, "evidence": {"dm": "Нет"}}, ["card_trop"], evidence_mode=True)

    def test_llm_quote_cannot_override_parser(self):
        text = "Первичный осмотр:\nАД 120/80"
        final, record, _, _, _ = resolve(text)
        quotes, sources = collect_service_evidence(record, final, {"bp": "120/80"}, {"bp": "invented"}, source_text=text)
        self.assertEqual(quotes["bp"], record["meta"]["bp"]["evidence"])
        self.assertEqual(sources["bp"], "parser")

    def test_consistency_cannot_override_confirmed_parser(self):
        text = "КАГ:\nВыполнена КАГ\nLAD: стеноз 95%"
        final, record, _, _, _ = resolve(text)
        record["values"]["ca_lad"] = 0
        record["meta"]["ca_lad"] = {"status": "confirmed", "source": "parser", "evidence": "LAD: стеноз 95%"}
        with self.assertRaises(FinalizationError):
            apply_clinical_consistency(final, record, text)

    def test_unknown_binary_quote_not_accepted_as_positive(self):
        quote = "Не курит"
        accepted, rejected = validate_llm_evidence({"smoking": {"code": 1, "text": "Курит"}}, {"smoking": quote}, "Анамнез:\n" + quote)
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, ["smoking"])

    def test_client_structured_evidence_transport_without_real_http(self):
        text = "Анализы крови:\nТропонин положительный."
        record = create_empty_template()
        request = build_llm_request(text, record, include_evidence=True)
        values = {k: deepcopy(FIELD_CONTRACT[k]["missing_value"]) for k in request["target_fields"]}
        proposed = {k: None for k in values}
        values["card_trop"], proposed["card_trop"] = "положительный", "Тропонин положительный."
        payload = {"values": values, "evidence": proposed}
        raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()
        transport = Mock(return_value=io.BytesIO(raw))
        config = YandexConfig("mock-placeholder", "gpt://mock-folder/mock-model", "mock-folder")
        response = YandexLLMClient(config=config, transport=transport).call(request)
        self.assertEqual(response, payload)
        body = json.loads(transport.call_args.args[0].data)
        self.assertEqual(set(body["response_format"]["json_schema"]["schema"]["properties"]), {"values", "evidence"})
        self.assertEqual(transport.call_count, 1)

    def test_network_guard_blocks_connections(self):
        with patch.object(socket, "create_connection", deny_network), self.assertRaises(RuntimeError):
            socket.create_connection(("example.invalid", 443))


class ServicePipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / (".test-run-clarifications-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)

    def test_full_service_pipeline_quotes_separate_and_logs_safe(self):
        source, result_file, service_file, log = [self.folder / name for name in ("input.md", "result.json", "service.json", "log.jsonl")]
        text = "Поступил 01/01/2020\nДиагноз:\nКод МКБ-10 I21.0\nКАГ:\nВыполнена КАГ\nПМЖВ: гемодинамически значимого стеноза нет\nАнализы крови:\nТропонин положительный.\nРекомендации:\nАспирин 100 мг 1 раз в сутки."
        source.write_text(text, encoding="utf-8")

        def fake_llm(request):
            values = {k: deepcopy(FIELD_CONTRACT[k]["missing_value"]) for k in request["target_fields"]}
            quotes = {k: None for k in values}
            values.update(ca_lad=2, aspirin=[{"name": "Аспирин", "prescription": "100 мг 1 раз в сутки"}])
            quotes.update(ca_lad="ПМЖВ: стеноз 95%", aspirin="Аспирин 100 мг 1 раз в сутки.")
            return {"values": values, "evidence": quotes}

        with patch("batch_runner.call_llm", side_effect=fake_llm):
            processed = batch_runner.process_single_file(source, result_file, log_file=log, service_file=service_file)
        self.assertTrue(processed.success, processed.error)
        final = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(len(final), 50)
        self.assertNotIn("evidence", final)
        self.assertEqual(final["admission_date"], "01.01.2020")
        self.assertEqual(final["ca_lad"], 0)
        self.assertEqual(final["mi_localisation"], {"code": 1, "text": "передней стенки"})
        self.assertEqual(processed.evidence["aspirin"], "Аспирин 100 мг 1 раз в сутки.")
        self.assertEqual(processed.meta["evidence_sources"]["aspirin"], "llm")
        self.assertEqual(processed.meta["evidence_sources"]["mi_localisation"], "icd_fallback")
        self.assertIn("ca_lad", processed.meta["rejected_evidence_fields"])
        self.assertTrue(all(quote in text for quote in processed.evidence.values()))
        service = json.loads(service_file.read_text(encoding="utf-8"))
        self.assertEqual(service["result"], final)
        self.assertEqual(processed.to_service_result()["evidence"], service["evidence"])
        logged = json.loads(log.read_text(encoding="utf-8"))
        self.assertNotIn("result", logged)
        self.assertNotIn("evidence", logged)
        self.assertNotIn("Тропонин", log.read_text(encoding="utf-8"))

    def test_service_path_collision_does_not_overwrite_source(self):
        source = self.folder / "input.md"
        source.write_text("Исходный текст", encoding="utf-8")
        with patch("batch_runner.call_llm") as llm:
            result = batch_runner.process_single_file(source, self.folder / "final.json", service_file=source)
        self.assertFalse(result.success)
        self.assertEqual(source.read_text(encoding="utf-8"), "Исходный текст")
        llm.assert_not_called()

    def test_failed_official_save_rolls_back_service_result(self):
        source, output, service = [self.folder / name for name in ("input.md", "out.json", "service.json")]
        source.write_text("Нет клинических сведений", encoding="utf-8")
        from json_io import atomic_write_json

        def fake_llm(request):
            values = {k: deepcopy(FIELD_CONTRACT[k]["missing_value"]) for k in request["target_fields"]}
            return {"values": values, "evidence": {k: None for k in values}}

        def fail_official(path, content):
            if Path(path) == output:
                raise OSError("simulated official save failure")
            atomic_write_json(path, content)

        for previous in (None, {"previous": True}):
            with self.subTest(previous=previous):
                if previous is not None:
                    atomic_write_json(service, previous)
                with patch("batch_runner.call_llm", side_effect=fake_llm), \
                     patch("batch_runner.atomic_write_json", side_effect=fail_official):
                    result = batch_runner.process_single_file(source, output, service_file=service)
                self.assertFalse(result.success)
                self.assertFalse(output.exists())
                if previous is None:
                    self.assertFalse(service.exists())
                else:
                    self.assertEqual(json.loads(service.read_text(encoding="utf-8")), previous)
