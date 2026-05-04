import re
import html
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

    def _normalize_html_text(self, html_text: str) -> str:
        """
        Normaliza HTML da página para regex:
        - decodifica entidades (ex.: &#xc7; -> Ç)
        - remove tags
        - remove acentos (ASCII fold)
        - colapsa espaços
        :param html_text: O texto HTML bruto extraído da página.
        :return: O texto normalizado
        """
        decoded_html = html.unescape(html_text or "")
        no_tags = re.sub(r"<[^>]+>", " ", decoded_html)
        normalized = self._normalize_text(no_tags)
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Métod0 auxiliar para normalizar texto, removendo acentos e caracteres especiais, e colapsando espaços.
         Pode ser usado tanto para o texto extraído da página quanto para as células das tabelas,
         para facilitar a comparação e extração de informações.
        :param text: O texto a ser normalizado
        :return: O texto normalizado, sem acentos, caracteres especiais, e com espaços colapsados.
        """
        return (
            unicodedata.normalize("NFKD", text)
            .encode("ascii", "ignore")
            .decode("ascii")
            .replace("\u00a0", " ")
        )

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
                text = page.get_text("html", sort=True)
                normalized_text = self._normalize_html_text(text)

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
                        f"Todos os campos já foram extraídos. "
                        f"Interrompendo na página {page_index}."
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

                # extrai tabelas e tenta extrair campos de interesse
                page_tables = [table.to_markdown() for table in page.find_tables()]
                self.try_extracting_fields(
                    page_tables, labor_claim_info, pattern_matches
                )
            logger.info(
                "[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tExtração concluída,"
                " iterei todas as páginas do PDF."
            )

        return labor_claim_info

    def try_extracting_fields(
        self,
        page_tables: list[str],
        labor_claim_info: LaborClaimInfo,
        matched_fields: list[str],
    ) -> LaborClaimInfo:
        """
        Tenta extrair os campos de interesse a partir das tabelas de uma página.
        """
        found_fields = []
        for table in page_tables:
            # caso todos os campos já tenham sido extraídos, não precisa continuar tentando nas tabelas restantes
            if not matched_fields:
                break
            for field_name in matched_fields:
                if labor_claim_info.get(field_name, None):
                    continue
                extracted = self.extract_field_value(
                    self._normalize_text(table), field_name
                )
                if extracted:  # evita sobrescrever com None
                    labor_claim_info[field_name] = extracted
                    found_fields.append(field_name)
            for field in found_fields:
                matched_fields.remove(field)
            found_fields = []
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

        # Remove o que não é dígito, vírgula ou parênteses, e trata o valor como negativo se estiver entre parênteses
        cleaned = re.sub(r"[^\d,()]", "", raw_value).strip()
        if not cleaned:
            return None
        # Verifica se o valor é negativo (entre parênteses) e prepara a string para conversão removendo os parênteses
        is_negative = cleaned.startswith("(") and cleaned.endswith(")")
        # Remove os parênteses, pontos de milhar e substitui a vírgula decimal por ponto para o formato Decimal
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

    def extract_field_values_broken_table(
        self, table: str, field_pattern: str
    ) -> Decimal | None:
        """
        Fallback para casos onde a estrutura de tabela é quebrada, com labels e valores misturados ou sem
         separação clara.
        Tenta encontrar o label em qualquer parte do texto e extrair o valor monetário mais próximo que
         apareça depois dele.
        :param table: O texto da tabela, com quebras de linha e espaços limpos.
        :param field_pattern: Pattern para encontrar label
        :return: valor decimal
        """
        flat_text = re.sub(r"<br\s*/?>", "\n", table, flags=re.IGNORECASE)
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

    def extract_field_value(self, table: str, field_name: str) -> Decimal | None:
        """
        Extrai o valor de um campo específico a partir das tabelas extraídas do PDF, usando um padrão de label.
        :param table: O texto da tabela, com quebras de linha e espaços limpos.
        :param field_name: O nome do campo a ser extraído, que deve corresponder a uma chave no field_pattern_map.
        """
        if field_name == "liquido_devido_ao_advogado":
            honorarios_total = self._extract_honorarios_demonstrativo_total(table)
            if honorarios_total is not None:
                return honorarios_total

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
                    return self._to_decimal(raw_value)
                break

        return self.extract_field_values_broken_table(table, field_pattern)


if __name__ == "__main__":
    data_path = Path(__file__).parents[2] / "data" / "Documentos"

    extractor = LaborClaimCalculationExtractor()

    result = extractor.extract(data_path / "0000337-81.2023.5.17.0002.pdf")
    print(result)

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
