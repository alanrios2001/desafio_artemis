import json
import unittest

from decimal import Decimal
from pathlib import Path

from src.extractors.labor_claim_calculation_extractor import (
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

    def test_full_extraction(self):
        pdf_files_path = Path(__file__).parents[1] / "data" / "Documentos"
        with open(
            self.data_path / "full_extraction_test_cases.jsonl",
            "r",
            encoding="utf-8-sig",
        ) as f:
            test_cases = [json.loads(line) for line in f]

        for item in test_cases:
            file = pdf_files_path / item["pdf_name"]

            expected_data = {
                key: Decimal(value) for key, value in item["expected"].items()
            }
            extracted_data = self.extractor.extract(file)

            for key, expected_value in expected_data.items():
                self.assertEqual(expected_value, extracted_data[key])
