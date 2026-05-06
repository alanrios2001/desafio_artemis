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
                r"|TOTAL\s+DA\s+RECLAMAD[AO]\s+APOS\s+DEDUCOES"
                r"|DEBITO\s+TOTAL\s+D[OA]\s+RECLAMAD[AO]"
                r"(?:\s+EM\s+\d{1,2}/(?:[A-Z]{3}|\d{1,2})/\d{2,4})?)"
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
                r"(?:DEMONSTRATIVO\s+DE\s+HONORARIOS"
                r"|NOME\s*:\s*HONORARIOS\s+DEVIDOS\s+PELO\s+RECLAMADO)"
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
                    try:
                        page_tables = [
                            table.to_markdown() for table in page.find_tables()
                        ]
                        self._extract_fields_from_tables(
                            page_tables, labor_claim_info, remaining_pattern_matches
                        )
                    except Exception as e:
                        logger.warning(
                            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                            f"Erro ao extrair tabelas da página {page_index}: {e}"
                        )

            remaining_fields = [
                field
                for field in self.field_pattern_map
                if field not in labor_claim_info
            ]
            if remaining_fields:
                for field in remaining_fields:
                    labor_claim_info[field] = Decimal(0)
                logger.warning(
                    f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                    "Extração concluída, mas os seguintes campos não foram encontrados: "
                    f"{', '.join(remaining_fields)}."
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
            return self._extract_honorarios_demonstrativo_total(table)

        if field_name == "valor_do_fgts":
            return self._extract_fgts_field_value(table)

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

    def _extract_honorarios_demonstrativo_total(self, text: str) -> Decimal | None:
        """
        Extrai honorários do advogado apenas do Demonstrativo de Honorários.

        A extração considera somente blocos "NOME: HONORARIOS DEVIDOS PELO RECLAMADO"
        e soma ocorrências de honorários advocatícios/sucumbenciais. Isso evita
        capturar honorários periciais ou totais gerais sem discriminação.

        :param text: Texto normalizado da página (xhtml) ou tabela Markdown.
        :return: Soma dos honorários devidos ao advogado, ou None se não encontrado.
        """
        blocks = self._extract_honorarios_reclamado_blocks(text)
        if not blocks:
            return None

        total = Decimal("0")
        found_value = False
        target_re = re.compile(
            r"HONORARIOS\s+(?:ADVOCATICIOS|(?:DE\s+)?SUCUMBENCIA)", re.IGNORECASE
        )

        for block in blocks:
            found_in_block = False
            non_empty_lines = [line for line in block.splitlines() if line.strip()]

            if len(non_empty_lines) > 1:
                for raw_line in non_empty_lines:
                    line_text = re.sub(r"[|*]", " ", raw_line)
                    line_text = re.sub(r"\s+", " ", line_text).strip()

                    if not target_re.search(line_text):
                        continue

                    cells = self._extract_line_cells(raw_line)
                    value = (
                        self._extract_last_money_from_cells(cells)
                        if cells
                        else self._extract_last_money_from_line(line_text)
                    )

                    if value is None:
                        continue

                    total += value
                    found_value = True
                    found_in_block = True

            if found_in_block:
                continue

            sanitized_block = re.sub(r"[|*]", " ", block)
            sanitized_block = re.sub(r"\s+", " ", sanitized_block).strip()

            for target_match in target_re.finditer(sanitized_block):
                value = self._extract_money_closest_to_span(
                    sanitized_block, target_match.start(), target_match.end()
                )
                if value is None:
                    continue

                total += value
                found_value = True

        return total if found_value else None

    @staticmethod
    def _extract_honorarios_reclamado_blocks(text: str) -> list[str]:
        """
        Recorta blocos do Demonstrativo de Honorários devidos pelo reclamado.

        O recorte é tolerante a texto corrido (xhtml normalizado) e a linhas de
        tabela Markdown, evitando depender exclusivamente de quebras de linha.

        :param text: Texto normalizado da página ou tabela.
        :return: Lista de blocos do demonstrativo de honorários do reclamado.
        """
        cleaned_text = re.sub(r"[|*]", " ", text)
        cleaned_text = re.sub(r"[ \t\r\f\v]+", " ", cleaned_text).strip()

        if not re.search(
            r"DEMONSTRATIVO\s+DE\s+HONORARIOS", cleaned_text, re.IGNORECASE
        ):
            return []

        demonstrativo_re = re.compile(
            r"DEMONSTRATIVO\s+DE\s+HONORARIOS(?P<body>.*?)(?=DEMONSTRATIVO\s+DE\s+|$)",
            re.IGNORECASE | re.DOTALL,
        )
        reclamado_block_re = re.compile(
            r"NOME\s*:\s*HONORARIOS\s+DEVIDOS\s+PELO\s+RECLAMADO(?P<body>.*?)(?=NOME\s*:|$)",
            re.IGNORECASE | re.DOTALL,
        )

        blocks: list[str] = []
        for demonstrativo_match in demonstrativo_re.finditer(cleaned_text):
            demonstrativo_body = demonstrativo_match.group("body")
            for block_match in reclamado_block_re.finditer(demonstrativo_body):
                blocks.append(block_match.group("body"))

        return blocks

    @staticmethod
    def _extract_money_closest_to_span(
        text: str, span_start: int, span_end: int
    ) -> Decimal | None:
        """
        Retorna o valor monetário mais próximo de um intervalo de texto.

        Útil para linhas longas/tabelas em que o label e o valor não estão em
        células separadas de forma confiável.
        """
        money_matches = list(re.finditer(MONEY_RE, text, re.IGNORECASE))
        if not money_matches:
            return None

        before_matches = [match for match in money_matches if match.end() <= span_start]
        if before_matches:
            closest_before = min(
                before_matches, key=lambda match: span_start - match.end()
            )
            return to_decimal(closest_before.group(0))

        after_matches = [match for match in money_matches if match.start() >= span_end]
        if not after_matches:
            return None

        closest_after = min(after_matches, key=lambda match: match.start() - span_end)
        return to_decimal(closest_after.group(0))

    @staticmethod
    def _extract_last_money_from_line(line: str) -> Decimal | None:
        """
        Extrai o último valor monetário de uma linha.

        Em linhas como:
        30/04/2025 30.385,04 15,00 % 4.557,76 HONORARIOS ADVOCATICIOS ...

        o último valor monetário é o valor calculado dos honorários.

        :param line: Linha de texto.
        :return: Último valor monetário da linha, ou None.
        """
        money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
        if not money_matches:
            return None

        return to_decimal(money_matches[-1].group(0))

    @staticmethod
    def _extract_last_money_from_cells(cells: list[str]) -> Decimal | None:
        """
        Extrai o último valor monetário encontrado nas células de uma linha Markdown.
        :param cells: Células extraídas de uma linha de tabela.
        :return: Último valor monetário encontrado, ou None.
        """
        for cell in reversed(cells):
            money_match = re.search(MONEY_RE, cell, re.IGNORECASE)
            if money_match:
                return to_decimal(money_match.group(0))

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

    def _extract_fgts_field_value(self, text: str) -> Decimal | None:
        """
        Extrai o valor final de FGTS.

        A ordem de prioridade é:
        1. linha cujo label seja exatamente 'FGTS';
        2. linha 'TOTAL DEVIDO AO AUTOR', usando a coluna FGTS.

        Isso evita capturar linhas intermediárias como:
        - FGTS 8% 6.886,94 15.650,90 8.763,96
        - MULTA SOBRE FGTS 40% 2.607,33 5.970,54 3.363,21
        - DIFERENCA DE FGTS DO CONTRATO 0,00 0,00 0,00 2.259,62 0,00 2.259,62

        :param text: Texto normalizado da página ou tabela.
        :return: Valor de FGTS convertido para Decimal, ou None se não for encontrado.
        """
        exact_fgts_value = self._extract_exact_fgts_line_value(text)
        if exact_fgts_value is not None:
            return exact_fgts_value

        return self._extract_fgts_from_total_devido_ao_autor(text)

    @staticmethod
    def _extract_exact_fgts_line_value(text: str) -> Decimal | None:
        """
        Extrai o valor de linhas cujo label seja exatamente 'FGTS'.

        Aceita casos como:
        - FGTS 21.621,44
        - 21.621,44 FGTS
        - | FGTS | 21.621,44 |

        Descarta casos como:
        - FGTS 8% 6.886,94
        - MULTA SOBRE FGTS 40% 2.607,33

        :param text: Texto normalizado da página ou tabela.
        :return: Valor de FGTS convertido para Decimal, ou None se não for encontrado.
        """
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
            if not money_matches:
                continue

            label_without_values = re.sub(MONEY_RE, " ", line, flags=re.IGNORECASE)
            label_without_values = re.sub(r"[|*:]", " ", label_without_values)
            label_without_values = re.sub(r"\s+", " ", label_without_values).strip()

            if not re.fullmatch(r"FGTS", label_without_values, flags=re.IGNORECASE):
                continue

            return to_decimal(money_matches[0].group(0))

        return None

    @staticmethod
    def _extract_fgts_from_total_devido_ao_autor(text: str) -> Decimal | None:
        """
        Extrai o FGTS em tabelas que possuem colunas monetárias, como:

        PEDIDOS | VLR. PRINC | VLR. CORRECAO | JUROS | FGTS | JUROS FGTS | TOTAL
        TOTAL DEVIDO AO AUTOR 43.945,62 5.003,02 16.888,37 5.887,51 2.041,93 73.766,45

        Nesse layout, o valor correto de FGTS é o 4º valor monetário da linha
        'TOTAL DEVIDO AO AUTOR', pois corresponde à coluna FGTS.

        :param text: Texto normalizado da página ou tabela.
        :return: Valor da coluna FGTS na linha total, ou None se não for encontrado.
        """
        if not re.search(r"\bFGTS\b", text, re.IGNORECASE):
            return None

        if not re.search(r"\bJUROS\s+FGTS\b", text, re.IGNORECASE):
            return None

        total_devido_ao_autor_re = re.compile(
            r"\bTOTAL\s+DEVIDO\s+AO\s+AUTOR\b", re.IGNORECASE
        )

        for raw_line in text.splitlines():
            line = re.sub(r"[|*]", " ", raw_line)
            line = re.sub(r"\s+", " ", line).strip()

            if not total_devido_ao_autor_re.search(line):
                continue

            money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))

            # Esperado:
            # VLR. PRINC, VLR. CORRECAO, JUROS, FGTS, JUROS FGTS, TOTAL
            if len(money_matches) < 6:
                continue

            fgts_match = money_matches[3]
            return to_decimal(fgts_match.group(0))

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
        if field_name == "liquido_devido_ao_advogado":
            return self._extract_honorarios_demonstrativo_total(text)

        if field_name == "valor_do_fgts":
            return self._extract_fgts_field_value(text)

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

            for money_match in money_matches[::-1]:
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
            # "0000380-42.2023.5.05.0005.pdf",
            "1001298-45.2023.5.02.0059 - Perito.pdf",
            "1001298-45.2023.5.02.0059 - Reclamada.pdf",
        ]
        pdf_files = list(data_path.glob("*.pdf"))
        for pdf_file in pdf_files:
            if any(ignored in str(pdf_file) for ignored in ignore):
                continue
            print(extractor.extract(pdf_file))

    print(extractor.extract(data_path / "0000380-42.2023.5.05.0005.pdf"))

    # run_all_pdfs()
