import re
import fitz
import pymupdf.layout

from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from utils.cast_utils import to_decimal
from utils.general_utils import get_logger
from utils.text_utils import normalize_text, normalize_html_text

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
        self.separator_re = re.compile(r"^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")
        # patterns para extração de cada campo, novos casos podem ser adicionados aqui para melhorar a cobertura,
        # buscando variações comuns de labels encontrados nos PDFs
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
                r"(?:IRRF\s+DEVIDO\s+PELO\s+RECLAMANTE"
                r"|IRRF\s+SOBRE\s+HONORARIOS(?:\s+PARA(?:\s+.+)?)?"
                r"|IMPOSTO\s+DE\s+RENDA)"
            ),
            "valor_do_fgts": (
                r"(?:FGTS"
                r"|DIFERENCA\s+DE\s+FGTS\s+DO\s+CONTRATO"
                r"|TOTAL\s+DO\s+FGTS)"
            ),
        }

    def extract(self, pdf_path: str | Path) -> LaborClaimInfo:
        """
        Extrai as informações contábeis navegando pelas paginas, a partir da extração de texto e tabelas do pdf,
         organizando por página.
        :param pdf_path: Caminho para o arquivo PDF a ser processado.
        :return:
        """
        pdf_path = Path(pdf_path)
        pdf_name = pdf_path.name
        logger.info(
            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tIniciando extração de informações."
        )

        if not pdf_path.exists():
            logger.error(f" PDF:{pdf_name} arquivo não encontrado")
            raise FileNotFoundError(f" PDF:{pdf_name} arquivo não encontrado")

        labor_claim_info: LaborClaimInfo = {}

        logger.info(
            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tPercorrendo paginas em busca"
            " dos campos de interesse."
        )
        with fitz.open(pdf_path) as document:
            for page_index, page in enumerate(document, start=1):
                text = page.get_text("xhtml", sort=True)
                normalized_text = normalize_html_text(text)

                if not normalized_text:
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Página {page_index} do sem texto extraído. "
                        "Pulando para a próxima página."
                    )
                    continue

                # melhora eficiência verificando labels pendentes antes de extrair tabelas, e das pendentes
                # quais aparecem no texto da página, para decidir se vale a pena tentar extrair tabelas daquela página
                pending_patterns, pattern_matches = (
                    self._get_pending_patterns_and_matches(
                        labor_claim_info, normalized_text
                    )
                )

                if not pending_patterns:
                    logger.info(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Extração concluída, interrompi na página {page_index}."
                    )
                    break

                # verifica se algum dos padrões pendentes aparece no texto da página antes de tentar extrair tabelas
                if not pattern_matches:
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Página {page_index} não contém labels pendentes. "
                        "Pulando para a próxima página."
                    )
                    continue

                # tenta extrair primeiro pelo texto HTML/XHTML limpo da página
                self._extract_fields_from_text(
                    normalized_text, labor_claim_info, pattern_matches
                )

                # recalcula os campos ainda pendentes na página atual
                _, remaining_pattern_matches = self._get_pending_patterns_and_matches(
                    labor_claim_info, normalized_text
                )

                # usa tabelas apenas como fallback para o que ainda não foi encontrado
                if remaining_pattern_matches:
                    page_tables = [table.to_markdown() for table in page.find_tables()]
                    self._extract_fields_from_tables(
                        page_tables, labor_claim_info, remaining_pattern_matches
                    )
            if pending_patterns:
                for pattern, _ in pending_patterns:
                    labor_claim_info[pattern] = Decimal(0)
                logger.warning(
                    f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                    "Extração concluída, mas os seguintes campos não foram encontrados: "
                    f"{', '.join([field for field, _ in pending_patterns])}."
                )

        return labor_claim_info

    def _get_pending_patterns_and_matches(
        self, labor_claim_info: LaborClaimInfo, text: str
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """
        Similar a _get_pending_patterns, mas também verifica se os padrões pendentes aparecem no texto da página.
         Isso pode ajudar a decidir mais rapidamente se vale a pena tentar extrair tabelas daquela página.
        :param labor_claim_info: O dicionário atual de informações extraídas, usado para determinar quais
         campos ainda estão pendentes.
        :param text: O texto da página atual, usado para verificar a presença dos padrões pendentes.
        :return: Uma lista de tuplas (field_name, pattern) para os campos que ainda estão pendentes
         e cujos padrões aparecem no texto.
        """
        pending_patterns = [
            (field, pattern)
            for field, pattern in self.field_pattern_map.items()
            if field not in labor_claim_info
        ]

        matched_fields = [
            field
            for field, pattern in pending_patterns
            if re.search(pattern, text, re.IGNORECASE)
        ]
        return pending_patterns, matched_fields

    def _extract_fields_from_tables(
        self,
        page_tables: list[str],
        labor_claim_info: LaborClaimInfo,
        matched_fields: list[str],
    ) -> LaborClaimInfo:
        """
        Tenta extrair os campos de interesse a partir das tabelas de uma página.
        :param page_tables: Lista de tabelas extraídas da página, em formato Markdown.
        :param labor_claim_info: Dicionário atual de informações extraídas, usado para evitar
        :param matched_fields: Lista de campos que existem na pagina.
        :return: Dicionário atualizado com os campos extraídos das tabelas.
        """
        found_fields = []
        for table in page_tables:
            # caso todos os campos já tenham sido extraídos, não precisa continuar tentando nas tabelas restantes
            if not matched_fields:
                break
            for field_name in matched_fields:
                if field_name in labor_claim_info:
                    continue
                extracted = self._extract_field_value_from_table(
                    normalize_text(table), field_name
                )
                if extracted is not None:  # None explicito evita falsy como Decimal(0)
                    labor_claim_info[field_name] = extracted
                    found_fields.append(field_name)
            for field in found_fields:
                matched_fields.remove(field)
            found_fields = []
        return labor_claim_info

    def _extract_field_value_from_table(
        self, table: str, field_name: str
    ) -> Decimal | None:
        """
        Extrai o valor de um campo específico a partir das tabelas extraídas do PDF, usando um padrão de label.
        :param table: O texto da tabela, com quebras de linha e espaços limpos.
        :param field_name: O nome do campo a ser extraído, que deve corresponder a uma chave no field_pattern_map.
        """
        if field_name == "liquido_devido_ao_advogado":
            honorarios_total = self._extract_honorarios_demonstrativo_total(table)
            if honorarios_total is not None:
                return honorarios_total

        if field_name == "valor_do_fgts":
            extracted_fgts = self._extract_fgts_field_value(table)
            if extracted_fgts is not None:
                return extracted_fgts

        field_pattern = self.field_pattern_map.get(field_name)
        if not field_pattern:
            return None

        label_re = re.compile(
            rf"^\*{{0,2}}\s*{field_pattern}\s*\*{{0,2}}\s*$", re.IGNORECASE
        )
        money_re = re.compile(
            rf"^\*{{0,2}}\s*({MONEY_RE})\s*\*{{0,2}}\s*$", re.IGNORECASE
        )

        for raw_line in table.splitlines():
            cells = self._extract_line_cells(raw_line)
            if not cells:
                continue
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
                    return to_decimal(raw_value)
                break

        return self._extract_money_on_same_line_as_label(table, field_pattern)

    def _extract_honorarios_demonstrativo_total(self, table: str) -> Decimal | None:
        """
        Extração específica para o caso dos honorários advocatícios no demonstrativo de cálculos
        :param table: O texto da tabela, com quebras de linha e espaços limpos.
        :return: O valor total dos honorários advocatícios, ou None se não for encontrado.
        """
        if "DEMONSTRATIVO DE HONORARIOS" not in table.upper():
            return None

        total_cell_re = re.compile(r"^\*{0,2}\s*TOTAL\s*\*{0,2}$", re.IGNORECASE)

        for raw_line in table.splitlines():
            cells = self._extract_line_cells(raw_line)
            if not cells:
                continue

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

    def _extract_line_cells(self, raw_line: str) -> list[str]:
        """
        Extrai as células de uma linha de tabela em formato Markdown, removendo tags HTML e normalizando espaços.
        :param raw_line: A linha bruta da tabela em formato Markdown, que pode conter tags HTML e formatação.
         Exemplo de linha: "| **TOTAL DEVIDO PELO RECLAMADO** | R$ 1.234,56 |"
        :return: Uma lista de strings representando as células da linha, com tags HTML removidas e espaços normalizados.
         Exemplo de retorno: ["TOTAL DEVIDO PELO RECLAMADO", "R$ 1.234,56"]
         Observação: Se a linha não for uma linha de tabela válida (não começar com "|" ou for uma linha de separação),
         retorna uma lista vazia.
        """
        line = raw_line.strip()
        if not line.startswith("|") or self.separator_re.match(line):
            return []

        cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in line.strip("|").split("|")]
        return [re.sub(r"\s+", " ", c).strip() for c in cells]

    @staticmethod
    def _extract_fgts_field_value(text: str) -> Decimal | None:
        """
        Extrai o valor final de FGTS, priorizando a linha cujo label seja exatamente 'FGTS'.

        Evita capturar valores intermediários como 'FGTS 8%', que normalmente representam
        uma verba específica do demonstrativo, não o total consolidado do campo solicitado.

        :param text: Texto normalizado da página ou tabela.
        :return: Valor de FGTS convertido para Decimal, ou None se não for encontrado.
        """
        exact_fgts_re = re.compile(
            r"^\s*(?:\|?\s*)?(?:FGTS)\s*(?:\|?\s*)?$", re.IGNORECASE
        )
        forbidden_fgts_re = re.compile(
            r"\bFGTS\s*8\s*%|\bMULTA\s+SOBRE\s+FGTS", re.IGNORECASE
        )

        for line in text.splitlines():
            if not line or forbidden_fgts_re.search(line):
                continue

            label_without_money = re.sub(MONEY_RE, " ", line, flags=re.IGNORECASE)
            label_without_money = re.sub(r"[ \t]+", " ", label_without_money).strip()

            if not exact_fgts_re.match(label_without_money):
                continue

            money_match = re.search(MONEY_RE, line, re.IGNORECASE)
            if money_match:
                return to_decimal(money_match.group(0))

        return None

    def _extract_fields_from_text(
        self, text: str, labor_claim_info: LaborClaimInfo, matched_fields: list[str]
    ) -> LaborClaimInfo:
        """
        Tenta extrair os campos de interesse diretamente do texto normalizado da página.
        :param text: Texto normalizado da página.
        :param labor_claim_info: Dicionário atual de informações extraídas.
        :param matched_fields: Campos pendentes cujos labels aparecem na página.
        :return: Dicionário atualizado.
        """
        for field_name in list(matched_fields):
            if field_name in labor_claim_info:
                continue

            extracted = self._extract_field_value_from_text(text, field_name)

            if extracted is not None:
                labor_claim_info[field_name] = extracted
                matched_fields.remove(field_name)

        return labor_claim_info

    def _extract_field_value_from_text(
        self, text: str, field_name: str
    ) -> Decimal | None:
        """
        Extrai o valor de um campo diretamente do texto normalizado da página.
        :param text: Texto normalizado da página.
        :param field_name: Nome do campo desejado.
        :return: Valor extraído como Decimal, ou None.
        """

        if field_name == "valor_do_fgts":
            extracted_fgts = self._extract_fgts_field_value(text)
            if extracted_fgts is not None:
                return extracted_fgts

        field_pattern = self.field_pattern_map.get(field_name)
        if not field_pattern:
            return None

        extracted_value = self._extract_money_on_same_line_as_label(text, field_pattern)

        if extracted_value is not None and field_name == "valor_de_irrf":
            return abs(extracted_value)

        return extracted_value

    @staticmethod
    def _extract_money_on_same_line_as_label(
        text: str, field_pattern: str
    ) -> Decimal | None:
        """
        Extrai um valor monetário que esteja na mesma linha do label.

        A busca aceita valor depois ou antes do label na mesma linha, mas descarta
        candidatos em outras linhas para evitar capturar valores de campos vizinhos.

        :param text: Texto normalizado da página ou tabela.
        :param field_pattern: Regex usado para localizar o label do campo.
        :return: Valor monetário convertido para Decimal, ou None se nada for encontrado.
        """
        label_re = re.compile(field_pattern, re.IGNORECASE)

        for line in text.splitlines():
            if not line:
                continue

            label_match = label_re.search(line)
            if not label_match:
                continue

            money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
            if not money_matches:
                continue

            candidates: list[tuple[int, str]] = []

            for money_match in money_matches:
                if money_match.end() <= label_match.start():
                    distance = label_match.start() - money_match.end()
                elif money_match.start() >= label_match.end():
                    distance = money_match.start() - label_match.end()
                else:
                    distance = 0

                candidates.append((distance, money_match.group(0)))

            _, raw_value = min(candidates, key=lambda item: item[0])
            return to_decimal(raw_value)

        return None


if __name__ == "__main__":

    data_path = Path(__file__).parents[2] / "data" / "Documentos"

    extractor = LaborClaimCalculationExtractor()

    def run_all_pdfs():
        ignore = [
            "0000380-42.2023.5.05.0005.pdf",
            "1001298-45.2023.5.02.0059 - Perito.pdf",
            "1001298-45.2023.5.02.0059 - Reclamada.pdf",
        ]
        pdf_files = list(data_path.glob("*.pdf"))
        for pdf_file in pdf_files:
            if any(ignored in str(pdf_file) for ignored in ignore):
                continue
            print(extractor.extract(pdf_file))

    # print(extractor.extract(data_path / "0000380-42.2023.5.05.0005.pdf"))

    run_all_pdfs()
