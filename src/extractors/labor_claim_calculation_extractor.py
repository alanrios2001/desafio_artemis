import re
import fitz
import pymupdf.layout
import unicodedata

from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from utils.general_utils import get_logger

logger = get_logger(__name__)

MONEY_RE = r"\(?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}\s*\)?"


class PageContent(TypedDict, total=False):
    text: str
    tables: list[str]


class LaborClaimInfo(TypedDict, total=False):
    """
    TypedDict para representar o dicionario de informações extraídas do pdf com calculo trabalhista
    """

    total_devido_pelo_reclamado: Decimal
    contribuicao_social_sobre_salarios_devido: Decimal
    liquido_devido_ao_reclamante: Decimal
    liquido_devido_ao_advogado: Decimal
    valor_de_irrf: Decimal
    valor_do_fgts: Decimal


class LaborClaimCalculationExtractor:
    def __init__(self) -> None:
        self.field_pattern_map = {
            "total_devido_pelo_reclamado": (
                r"(?:TOTAL\s+DEVIDO(?:\s+PELO)?\s+RECLAMAD[OA]"
                r"|TOTAL\s+DEVIDO\s+PELA\s+RECLAMAD[AO]"
                r"|TOTAL\s+DA\s+RECLAMAD[AO]\s+APOS\s+DEDUCOES)"
            ),
            "contribuicao_social_sobre_salarios_devido": (
                r"(?:CONTRIBUICAO\s+SOCIAL\s+SOBRE\s+SALARIOS\s+DEVID[OA]S?"
                r"|TOTAL\s+DA\s+CONTRIBUICAO\s+PREVIDENCIARIA)"
            ),
            "liquido_devido_ao_reclamante": (
                r"(?:LIQUIDO\s+DEVIDO\s+AO\s+RECLAMANTE"
                r"|TOTAL\s+LIQUIDO\s+DEVIDO\s+AO\s+AUTOR)"
            ),
            "liquido_devido_ao_advogado": (
                r"(?:LIQUIDO\s+DEVIDO\s+AO\s+ADVOGADO"
                r"|TOTAL\s+LIQUIDO\s+DEVIDO\s+AO\s+ADVOGADO"
                r"|HONORARIOS\s+LIQUIDOS?\s+PARA(?:\s+.+)?)"
            ),
            "valor_de_irrf": (
                r"(?:IRPF\s+DEVIDO\s+PELO\s+RECLAMANTE"
                r"|IRRF\s+DEVIDO\s+PELO\s+RECLAMANTE"
                r"|IMPOSTO\s+DE\s+RENDA)"
            ),
            "valor_do_fgts": (
                r"(?:FGTS"
                r"|DIFERENCA\s+DE\s+FGTS\s+DO\s+CONTRATO"
                r"|TOTAL\s+DO\s+FGTS)"
            ),
        }

    @staticmethod
    def _normalize_table_text(table: str) -> str:
        return (
            unicodedata.normalize("NFKD", table)
            .encode("ascii", "ignore")
            .decode("ascii")
            .replace("\u00a0", " ")
        )

    def _all_fields_extracted(self, labor_claim_info: LaborClaimInfo) -> bool:
        return all(
            field_name in labor_claim_info for field_name in self.field_pattern_map
        )

    @staticmethod
    def _extract_honorarios_demonstrativo_total(table: str) -> Decimal | None:
        normalized_table = (
            unicodedata.normalize("NFKD", table)
            .encode("ascii", "ignore")
            .decode("ascii")
            .replace("\u00a0", " ")
        )
        if "DEMONSTRATIVO DE HONORARIOS" not in normalized_table.upper():
            return None

        separator_re = re.compile(
            r"^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$", re.IGNORECASE
        )
        total_cell_re = re.compile(r"^\*{0,2}\s*TOTAL\s*\*{0,2}$", re.IGNORECASE)

        for raw_line in normalized_table.splitlines():
            line = raw_line.strip()
            if not line.startswith("|") or separator_re.match(line):
                continue

            cells = [
                re.sub(r"<[^>]+>", " ", c).strip() for c in line.strip("|").split("|")
            ]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]

            if not any(total_cell_re.match(c) for c in cells):
                continue

            for candidate in reversed(cells):
                m = re.search(MONEY_RE, candidate, re.IGNORECASE)
                if m:
                    cleaned = re.sub(r"[^\d,()]", "", m.group(0)).strip()
                    is_negative = cleaned.startswith("(") and cleaned.endswith(")")
                    numeric = cleaned.strip("()").replace(".", "").replace(",", ".")
                    try:
                        value = Decimal(numeric)
                        return -value if is_negative else value
                    except Exception:
                        return None
        return None

    def extract(self, pdf_path: str | Path) -> LaborClaimInfo:
        """
        Extrai as informações contábeis navegando pelas paginas, a partir da extração de texto e tabelas do pdf,
         organizando por página.
        :param pdf_path: Caminho para o arquivo PDF a ser processado.
        :return:
        """

        logger.info(
            f"[LaborClaimCalculationExtractor][extract] Iniciando extração de informações do PDF: {pdf_path}"
        )

        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF não encontrado: {pdf_path}")

        labor_claim_info: LaborClaimInfo = {}

        logger.info(
            "[LaborClaimCalculationExtractor][extract] Extraindo tabelas do PDF usando PyMuPDF..."
        )
        with fitz.open(pdf_path) as document:
            for page_index, page in enumerate(document, start=1):
                text = page.get_text("html", sort=True)
                if not text.strip():
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] Página {page_index} sem texto extraído. "
                        "Pulando para a próxima página."
                    )
                    continue

                # melhora eficiência verificando labels pendentes antes de extrair tabelas
                normalized_text = self._normalize_table_text(text)
                pending_patterns = [
                    pattern
                    for field, pattern in self.field_pattern_map.items()
                    if field not in labor_claim_info
                ]

                if pending_patterns and not any(
                    re.search(pattern, normalized_text, re.IGNORECASE)
                    for pattern in pending_patterns
                ):
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] Página {page_index} não contém labels pendentes. "
                        "Pulando para a próxima página."
                    )
                    continue

                tables = page.find_tables()
                page_tables = [table.to_markdown() for table in tables]
                self.try_extracting_fields(page_tables, labor_claim_info)

                if self._all_fields_extracted(labor_claim_info):
                    logger.info(
                        "[LaborClaimCalculationExtractor][extract] Todos os campos obrigatórios foram extraídos. "
                        f"Interrompendo na página {page_index}."
                    )
                    break

        return labor_claim_info

    def try_extracting_fields(
        self, page_tables: list[str], labor_claim_info: LaborClaimInfo
    ) -> LaborClaimInfo:
        """
        Tenta extrair os campos de interesse a partir das tabelas de uma página.
        """
        for table in page_tables:
            if self._all_fields_extracted(labor_claim_info):
                break
            for field_name in self.field_pattern_map:  # só campos que têm regex mapeada
                current_value = labor_claim_info.get(field_name, None)
                if current_value:
                    continue
                extracted = self.extract_field_value(table, field_name)
                if extracted:  # evita sobrescrever com None
                    labor_claim_info[field_name] = extracted

        return labor_claim_info

    @staticmethod
    def _to_decimal(raw_value: str) -> Decimal | None:
        """
        Converte uma string de valor monetário em um Decimal,
         lidando com formatos comuns e valores negativos entre parênteses.
        :param raw_value: A string bruta contendo o valor monetário, possivelmente com símbolos, espaços e formatação.
        :return: Um Decimal representando o valor monetário, ou None se a conversão falhar.
         Exemplo de formatos aceitos: "R$ 1.234,56", "(R$ 1.234,56)", "1234,56", "(1234,56)",
         "R$1.234,56", "1.234,56", "(1.234,56)"
        """
        cleaned = re.sub(r"[^\d,()]", "", raw_value).strip()
        if not cleaned:
            return None
        is_negative = cleaned.startswith("(") and cleaned.endswith(")")
        numeric = cleaned.strip("()").replace(".", "").replace(",", ".")
        try:
            value = Decimal(numeric)
            return -value if is_negative else value
        except Exception as e:
            logger.error(
                "[LaborClaimCalculationExtractor][_to_decimal] "
                f"Erro ao converter valor para Decimal: {e}"
            )
            return None

    def extract_field_value(self, table: str, field_name: str) -> Decimal | None:
        """
        Extrai o valor de um campo específico a partir das tabelas extraídas do PDF, usando um padrão de label.
        """
        if field_name == "liquido_devido_ao_advogado":
            honorarios_total = self._extract_honorarios_demonstrativo_total(table)
            if honorarios_total is not None:
                return honorarios_total

        field_pattern = self.field_pattern_map.get(field_name)
        if not field_pattern:
            return None

        normalized_table = self._normalize_table_text(table)

        label_re = re.compile(
            rf"^\*{{0,2}}\s*{field_pattern}\s*\*{{0,2}}\s*$", re.IGNORECASE
        )
        money_re = re.compile(
            rf"^\*{{0,2}}\s*({MONEY_RE})\s*\*{{0,2}}\s*$", re.IGNORECASE
        )
        separator_re = re.compile(r"^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")

        for raw_line in normalized_table.splitlines():
            line = raw_line.strip()
            if not line.startswith("|") or separator_re.match(line):
                continue

            cells = [
                re.sub(r"<[^>]+>", " ", c).strip() for c in line.strip("|").split("|")
            ]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]

            for idx, cell in enumerate(cells):
                if not label_re.match(cell):
                    continue

                for candidate in cells[idx + 1 :]:
                    m = money_re.match(candidate) or re.search(
                        MONEY_RE, candidate, re.IGNORECASE
                    )
                    if not m:
                        continue
                    raw_value = m.group(1) if m.lastindex else m.group(0)
                    return self._to_decimal(raw_value)
                break

        return self.extract_field_values_broken_table(normalized_table, field_pattern)

    def extract_field_values_broken_table(
        self, normalized_table: str, field_pattern: str
    ) -> Decimal | None:
        """
        Fallback para casos onde a estrutura de tabela é quebrada, com labels e valores misturados ou sem
         separação clara.
        Tenta encontrar o label em qualquer parte do texto e extrair o valor monetário mais próximo que
         apareça depois dele.
        :param normalized_table: O texto da tabela já normalizado, com quebras de linha e espaços limpos.
        :param field_pattern: Pattern para encontrar label
        :return: valor decimal
        """
        flat_text = re.sub(r"<br\s*/?>", "\n", normalized_table, flags=re.IGNORECASE)
        flat_text = flat_text.replace("|", "\n")
        flat_text = re.sub(r"\*\*", " ", flat_text)
        flat_text = re.sub(r"[ \t]+", " ", flat_text)

        label_anywhere_re = re.compile(field_pattern, re.IGNORECASE)
        for lm in label_anywhere_re.finditer(flat_text):
            window = flat_text[lm.end() : lm.end() + 250]
            vm = re.search(MONEY_RE, window, re.IGNORECASE)
            if vm:
                return self._to_decimal(vm.group(0))

        return None


if __name__ == "__main__":
    data_path = Path(__file__).parents[2] / "data" / "Documentos"

    extractor = LaborClaimCalculationExtractor()

    # result = extractor.extract(data_path / "0011084-61.2016.5.15.0109.pdf")

    ignore = [
        "0000380-42.2023.5.05.0005.pdf",
        "1001298-45.2023.5.02.0059 - Perito.pdf",
        "1001298-45.2023.5.02.0059 - Reclamada.pdf",
    ]

    pdf_files = list(data_path.glob("*.pdf"))
    for pdf_file in pdf_files:
        if any(ignored in str(pdf_file) for ignored in ignore):
            continue
        print(result := extractor.extract(pdf_file))
