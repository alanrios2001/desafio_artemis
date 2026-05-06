import json
import time
import unittest

from decimal import Decimal
from pathlib import Path

from src.extractors.labor_claim_calculation_extractor import (
    LaborClaimState,
    LaborClaimCalculationExtractor,
)


class TestLaborClaimCalculationExtractor(unittest.TestCase):
    def setUp(self):
        self.data_path = (
            Path(__file__).parents[0]
            / "data"
            / "labor_claim_calculation_extractor_test_cases"
        )
        self.extractor = LaborClaimCalculationExtractor()

    def _read_jsonl(self, file_name: str) -> list[dict]:
        with open(self.data_path / file_name, "r", encoding="utf-8-sig") as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def _to_decimal(value: str | None) -> Decimal | None:
        return Decimal(value) if value is not None else None

    def test_atomic_honorarios_extraction_cases(self):
        test_cases = self._read_jsonl("honorarios_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                extracted = self.extractor._extract_honorarios_demonstrativo_total(
                    item["text"]
                )
                self.assertEqual(self._to_decimal(item["expected"]), extracted)

    def test_atomic_irrf_extraction_cases(self):
        test_cases = self._read_jsonl("irrf_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                extracted = self.extractor._extract_irrf_field_value(item["text"])
                self.assertEqual(self._to_decimal(item["expected"]), extracted)

    def test_atomic_fgts_extraction_cases(self):
        test_cases = self._read_jsonl("fgts_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                extracted = self.extractor._extract_fgts_field_value(item["text"])
                self.assertEqual(self._to_decimal(item["expected"]), extracted)

    def test_atomic_contribuicao_social_extraction_cases(self):
        test_cases = self._read_jsonl("contribuicao_social_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                extracted = self.extractor._extract_contribuicao_social_value(
                    item["text"]
                )
                self.assertEqual(self._to_decimal(item["expected"]), extracted)

    def test_atomic_field_value_extraction_cases(self):
        test_cases = self._read_jsonl("field_value_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                extracted = self.extractor._extract_field_value_from_text(
                    item["text"], item["field_name"]
                )
                self.assertEqual(self._to_decimal(item["expected"]), extracted)

    def test_atomic_pending_pattern_matching_cases(self):
        test_cases = self._read_jsonl("pending_patterns_atomic_cases.jsonl")

        for item in test_cases:
            with self.subTest(case=item["case_name"]):
                _, matched_fields = self.extractor._get_pending_patterns_and_matches(
                    LaborClaimState(), item["text"]
                )

                if item["should_match"]:
                    self.assertIn(item["field"], matched_fields)
                else:
                    self.assertNotIn(item["field"], matched_fields)

    def test_full_extraction(self):
        pdf_files_path = self.data_path / "Documentos"
        test_cases = self._read_jsonl("full_extraction_test_cases.jsonl")

        for item in test_cases:
            file = pdf_files_path / item["pdf_name"]

            expected_data = {
                key: Decimal(value) if value else None
                for key, value in item["expected"].items()
            }
            start_time = time.time()
            extracted_data = self.extractor.extract(file)
            end_time = time.time()
            total_time = end_time - start_time
            print(item["pdf_name"], total_time, "seconds")

            for key, expected_value in expected_data.items():
                self.assertEqual(expected_value, extracted_data[key])
