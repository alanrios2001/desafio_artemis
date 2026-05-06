import json
import time
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
                "|15/12/2023|HONORARIOS PERICIAIS - ENGENHEIRO|LAUDO EMPRESTADO|3.500,00|1,019223195|3.567,28"
                "|-|3.567,28|",
                "|30/04/2025|HONORARIOS ADVOCATICIOS|RICARDO ARAUJO ALVES|30.385,04|30.385,04|15,00 %|15,00 %"
                "|4.557,76|",
                "|**Total**|**Total**|**Total**|**Total**|**Total**|**Total**|**Total**|**8.125,04**|",
            ]
        )

        extracted = self.extractor._extract_honorarios_demonstrativo_total(table_text)

        self.assertEqual(Decimal("4557.76"), extracted)

    def test_full_extraction(self):
        pdf_files_path = self.data_path / "Documentos"
        with open(
            self.data_path / "full_extraction_test_cases.jsonl",
            "r",
            encoding="utf-8-sig",
        ) as f:
            test_cases = [json.loads(line) for line in f]

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

    def test_extract_honorarios_sum_reclamante_and_reclamado_blocks(self):
        text = (
            "Demonstrativo de Honorarios Nome: HONORARIOS DEVIDOS PELO RECLAMANTE "
            "06/06/2022 28.780,54 1,001146708 28.813,54 11.420,53 40.234,07 HONORARIOS DE SUCUMBENCIA "
            "PATRONO DA RECLAMADA 40.234,07 Total Nome: HONORARIOS DEVIDOS PELO RECLAMADO "
            "10/01/2024 3.500,00 1,200800000 4.202,80 - 4.202,80 HONORARIOS PERICIAIS - ENGENHEIRO "
            "01/10/2025 1.117.993,92 15,00 % 167.699,09 HONORARIOS DE SUCUMBENCIA PATRONO DO RECLAMANTE "
            "171.901,89 Total Demonstrativo de Imposto de Renda"
        )

        extracted = self.extractor._extract_honorarios_demonstrativo_total(text)

        self.assertEqual(Decimal("207933.16"), extracted)
