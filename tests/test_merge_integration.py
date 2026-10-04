import json
from pathlib import Path
import shutil
import uuid
import unittest
from unittest.mock import patch

import batch_runner
from field_contract import FIELD_CONTRACT
from final_validator import validate_type
from pipeline_errors import FinalValidationError
from prompt_builder import build_llm_request
from parser import create_empty_template


class MergedPipelineTests(unittest.TestCase):
    def test_negative_smoking_preserved_end_to_end(self):
        phrases = ("Не курит", "Бросил курить", "Бросила курить",
                   "Прекратил курить", "Прекратила курить")
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                root = Path(__file__).parent / ("integration_" + uuid.uuid4().hex)
                root.mkdir()
                self.addCleanup(shutil.rmtree, root)
                source, output = root / "input.md", root / "output.json"
                source.write_text("Анамнез:\n" + phrase, encoding="utf-8")

                def checked_prompt(text, record, **options):
                    self.assertEqual(set(record), {"values", "meta"})
                    return build_llm_request(text, record, **options)

                def fake_llm(request):
                    values = {key: FIELD_CONTRACT[key]["missing_value"] for key in request["target_fields"]}
                    return {"values": values, "evidence": {key: None for key in values}}

                with patch("batch_runner.build_llm_request", side_effect=checked_prompt), \
                     patch("batch_runner.call_llm", side_effect=fake_llm):
                    result = batch_runner.process_single_file(source, output)
                self.assertTrue(result.success, result.error)
                values = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(tuple(values), tuple(FIELD_CONTRACT))
                self.assertEqual(values["smoking"], {"code": 0, "text": phrase})

    def test_zero_smoking_positive_text_rejected(self):
        with self.assertRaises(FinalValidationError):
            validate_type("smoking", {"code": 0, "text": "Курит"})

    def test_unresolved_record_rejects_evidence(self):
        record = create_empty_template()
        record["meta"]["bmi"]["evidence"] = "ИМТ 20"
        with self.assertRaises(ValueError):
            build_llm_request("Обезличенный текст", record)

    def test_confirmed_record_rejects_wrong_source(self):
        record = create_empty_template()
        record["values"]["bmi"] = 20
        record["meta"]["bmi"] = {"status": "confirmed", "source": "llm", "evidence": "ИМТ 20"}
        with self.assertRaises(ValueError):
            build_llm_request("Обезличенный текст", record)
