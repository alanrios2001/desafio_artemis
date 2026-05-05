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

    def test_extract_honorarios_from_demonstrativo_xhtml_normalized(self):
        text = (
            "Demonstrativo de Multas / Indenizacoes Nome: MULTAS / INDENIZACOES DEVIDAS AO RECLAMANTE "
            "Total Demonstrativo de Honorarios Nome: HONORARIOS DEVIDOS PELO RECLAMADO "
            "15/12/2023 3.500,00 1,019223195 3.567,28 - 3.567,28 HONORARIOS PERICIAIS - ENGENHEIRO "
            "30/04/2025 30.385,04 15,00 % 4.557,76 HONORARIOS ADVOCATICIOS RICARDO ARAUJO ALVES "
            "8.125,04 Total Demonstrativo de Imposto de Renda"
        )

        extracted = self.extractor._extract_honorarios_demonstrativo_total(text)

        self.assertEqual(Decimal("4557.76"), extracted)

    def test_extract_honorarios_from_demonstrativo_table_normalized(self):
        table_text = "\n".join(
            [
                "|Demonstrativo de Honorarios|Col2|Col3|Col4|Col5|Col6|Col7|Col8|",
                "|---|---|---|---|---|---|---|---|",
                "|** Nome: HONORARIOS DEVIDOS PELO RECLAMADO**|** Nome: HONORARIOS DEVIDOS PELO RECLAMADO**|",
                "|15/12/2023|HONORARIOS PERICIAIS - ENGENHEIRO|LAUDO EMPRESTADO|3.500,00|1,019223195|3.567,28|-|3.567,28|",
                "|30/04/2025|HONORARIOS ADVOCATICIOS|RICARDO ARAUJO ALVES|30.385,04|30.385,04|15,00 %|15,00 %|4.557,76|",
                "|**Total**|**Total**|**Total**|**Total**|**Total**|**Total**|**Total**|**8.125,04**|",
            ]
        )

        extracted = self.extractor._extract_honorarios_demonstrativo_total(table_text)

        self.assertEqual(Decimal("4557.76"), extracted)

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
