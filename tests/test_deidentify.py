import unittest

from deidentify import deidentify_text


class DeidentifyTests(unittest.TestCase):
    def test_removes_patient_header_identifiers(self):
        text = (
            "Кардиологическое отделение № 2. "
            "История болезни № 700001.\n"
            "Пациент: Карл Генрихович Марксов, "
            "д.р. 05.05.1818, возраст 201 год.\n"
        )

        redacted, audit = deidentify_text(text)

        self.assertNotIn("Карл Генрихович Марксов", redacted)
        self.assertNotIn("05.05.1818", redacted)
        self.assertNotIn("700001", redacted)
        self.assertNotIn("№ 2", redacted)

        self.assertIn("[REDACTED_PATIENT]", redacted)
        self.assertIn("[REDACTED_BIRTH_DATE]", redacted)
        self.assertIn("[REDACTED_AGE]", redacted)
        self.assertIn("[REDACTED_MEDICAL_RECORD_NUMBER]", redacted)
        self.assertIn("[REDACTED_DEPARTMENT_NUMBER]", redacted)

        self.assertTrue(audit["deidentified"])
        self.assertFalse(audit["raw_identifiers_retained"])

    def test_does_not_remove_medical_dates(self):
        text = (
            "Поступил 01.01.2020. "
            "Выписан 07.01.2020. "
            "ЭКГ от 01.01.2020."
        )

        redacted, audit = deidentify_text(text)

        self.assertIn("01.01.2020", redacted)
        self.assertIn("07.01.2020", redacted)
        self.assertEqual(audit["identifier_occurrences"], 0)

    def test_does_not_remove_medical_numbers(self):
        text = (
            "АД 147/91 мм рт. ст. "
            "ЧДД 16. "
            "ЧСС 84. "
            "креатинин 99 мкмоль/л."
        )

        redacted, audit = deidentify_text(text)

        self.assertIn("147/91", redacted)
        self.assertIn("креатинин 99", redacted)
        self.assertEqual(audit["identifier_occurrences"], 0)


if __name__ == "__main__":
    unittest.main()
