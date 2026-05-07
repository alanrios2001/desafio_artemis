import re
import fitz

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, TypeAlias
from pymupdf import Document, Page

from utils.cast_utils import to_decimal
from utils.general_utils import get_logger
from utils.text_utils import normalize_html_text

logger = get_logger(__name__)

MONEY_RE = r"\(?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}\s*\)?"

FieldName = Literal[
    "total_devido_pelo_reclamado",
    "contribuicao_social_sobre_salarios_devido",
    "liquido_devido_ao_reclamante",
    "liquido_devido_ao_advogado",
    "valor_de_irrf",
    "valor_do_fgts",
]

ALL_FIELDS: tuple[FieldName, ...] = (
    "total_devido_pelo_reclamado",
    "contribuicao_social_sobre_salarios_devido",
    "liquido_devido_ao_reclamante",
    "liquido_devido_ao_advogado",
    "valor_de_irrf",
    "valor_do_fgts",
)

LaborClaimInfo: TypeAlias = dict[str, Decimal | None]


@dataclass(slots=True)
class LaborClaimState:
    total_devido_pelo_reclamado: Decimal | None = None
    contribuicao_social_sobre_salarios_devido: Decimal | None = None
    liquido_devido_ao_reclamante: Decimal | None = None
    liquido_devido_ao_advogado: Decimal | None = None
    valor_de_irrf: Decimal | None = None
    valor_do_fgts: Decimal | None = None

    def has(self, field: FieldName) -> bool:
        # Consulta centralizada para saber se um campo já foi preenchido.
        return getattr(self, field) is not None

    def set(self, field: FieldName, value: Decimal) -> None:
        # Atualiza dinamicamente o atributo correspondente ao campo extraído.
        setattr(self, field, value)

    def missing_fields(self) -> list[FieldName]:
        # Retorna somente os campos ainda pendentes para orientar os próximos passos da extração.
        return [field for field in ALL_FIELDS if not self.has(field)]

    def to_dict(self) -> LaborClaimInfo:
        # Gera o payload final preservando a ordem definida em ALL_FIELDS.
        info: LaborClaimInfo = {}
        for field in ALL_FIELDS:
            info[field] = getattr(self, field)
        return info


class LaborClaimCalculationExtractor:
    """
    Classe responsável por extrair informações de cálculos de processos trabalhistas.\n
    Campos a serém extraídos:\n
    - total_devido_pelo_reclamado
    - contribuicao_social_sobre_salarios_devido
    - liquido_devido_ao_reclamante
    - liquido_devido_ao_advogado
    - valor_de_irrf
    - valor_do_fgts
    """

    def __init__(self) -> None:
        self.page_chunk_size = 3
        self.label_money_window = 150
        # patterns para extração de cada campo, novos casos podem ser adicionados aqui para melhorar a cobertura,
        # buscando variações comuns de labels encontrados nos PDFs
        self.field_pattern_map: dict[FieldName, str] = {
            "total_devido_pelo_reclamado": (
                r"(?:TOTAL\s+DEVIDO(?:\s+PELO)?\s+RECLAMAD[OA]"
                r"|TOTAL\s+DEVIDO\s+PELA\s+RECLAMAD[AO]"
                r"|TOTAL\s+DA\s+RECLAMAD[AO]\s+APOS\s+DEDUCOES"
                r"|DEBITO\s+TOTAL\s+D[OA]\s+RECLAMAD[AO]"
                r"|TOTAL\s+GERAL(?:\s+EM\s+\d{1,2}/(?:[A-Z]{3}|\d{1,2})/\d{2,4})?"
                r"(?:\s+EM\s+\d{1,2}/(?:[A-Z]{3}|\d{1,2})/\d{2,4})?)"
            ),
            "contribuicao_social_sobre_salarios_devido": (
                r"(?:CONTRIBUICAO\s+SOCIAL\s+SOBRE\s+SALARIOS\s+DEVID[OA]S?"
                r"|TOTAL\s+DA\s+CONTRIBUICAO\s+PREVIDENCIARIA"
                r"|INSS\s+COTA-EMPREGADOR"
                r"|INSS\s+(?:DO|DA|PARTE\s+DO|PARTE\s+DA)\s+RECLAMANT[EA]"
                r"|INSS\s+(?:DO|DA|PARTE\s+DO|PARTE\s+DA)\s+RECLAMAD[AO])"
            ),
            "liquido_devido_ao_reclamante": (
                r"(?:LIQUIDO\s+DEVIDO\s+AO\s+RECLAMANTE"
                r"|TOTAL\s+LIQUIDO\s+DEVIDO\s+AO\s+AUTOR"
                r"|CREDITO\s+LIQUIDO)"
            ),
            "liquido_devido_ao_advogado": (
                r"(?:DEMONSTRATIVO\s+DE\s+HONORARIOS"
                r"|NOME\s*:\s*HONORARIOS\s+DEVIDOS\s+PELO\s+RECLAMADO"
                r"|HONORARIOS\s+ADVOCATICIOS\s+DEVIDOS\s+PELA\s+RECLAMAD[AO]"
                r"|HONORARIOS\s+ADVOCATICIOS\s+AO\s+ADVOGADO\s+DO\s+RECTE"
                r"|HONORARIOS\s+DE\s+SUCUMBENCIA)"
            ),
            "valor_de_irrf": (
                r"(?:VALOR\s+TOTAL\s+DO\s+IRRF"
                r"|IRRF\s+DEVIDO\s+PELO\s+RECLAMANTE"
                r"|IRRF\s+DO\s+RECLAMANTE"
                r"|IMPOSTO\s+DE\s+RENDA\s+A\s+RECOLHER"
                r"|IMPOSTO\s+DE\s+RENDA"
                r"|DEMONSTRATIVO\s+DE\s+IMPOSTO\s+DE\s+RENDA)"
            ),
            "valor_do_fgts": r"FGTS",
        }
        self.special_field_extractors: dict[
            FieldName, Callable[[str], Decimal | None]
        ] = {
            "liquido_devido_ao_advogado": self._extract_honorarios_demonstrativo_total,
            "valor_do_fgts": self._extract_fgts_field_value,
            "contribuicao_social_sobre_salarios_devido": self._extract_contribuicao_social_value,
            "valor_de_irrf": self._extract_irrf_field_value,
        }

    @staticmethod
    def _is_soft_value_for_irrf(field_name: FieldName, value: Decimal | None) -> bool:
        """Permite revisar IRRF quando o valor atual é 0,00 e pode haver valor definitivo depois."""
        # Trata 0,00 de IRRF como valor provisório, permitindo sobrescrita posterior.
        return field_name == "valor_de_irrf" and value == Decimal("0")

    @staticmethod
    def _reorder_document_pages(document: Document) -> list[Page]:
        """
        Reordena as páginas alternando blocos do início e do fim do documento.

        Exemplo com bloco de 9:
        - páginas 1-9
        - últimas 9
        - próximas 9 do início
        - próximas 9 do fim
        - etc.

        :param document: Objeto documento contendo as páginas do PDF.
        :return: Lista de páginas reordenada.
        """
        split_len = 9
        total_pages = len(document)
        if total_pages == 0:
            return []

        ordered_indices: list[int] = []
        left = 0
        right = total_pages

        while left < right:
            # Consome um bloco do início...
            left_end = min(left + split_len, right)
            ordered_indices.extend(range(left, left_end))
            left = left_end

            if left >= right:
                break

            # e alterna com um bloco do fim, reduzindo o espaço de busca.
            right_start = max(right - split_len, left)
            ordered_indices.extend(range(right_start, right))
            right = right_start

        return [document[i] for i in ordered_indices]

    def extract(self, pdf_path: str | Path) -> LaborClaimInfo:
        """
        Extrai os campos contábeis do PDF a partir de texto XHTML normalizado.

        :param pdf_path: Caminho do arquivo PDF a ser processado.
        :return: Dicionário com os valores extraídos para todos os campos de interesse.
        """
        pdf_path = Path(pdf_path)
        pdf_name = pdf_path.name
        logger.info(
            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tIniciando extração de informações."
        )

        if not pdf_path.exists():
            logger.error(f" PDF:{pdf_name} arquivo não encontrado")
            raise FileNotFoundError(f" PDF:{pdf_name} arquivo não encontrado")

        labor_claim_state = LaborClaimState()

        logger.info(
            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tPercorrendo paginas em busca"
            " dos campos de interesse."
        )

        with fitz.open(pdf_path) as document:
            document = self._reorder_document_pages(document)
            total_pages = len(document)
            logger.debug(
                f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\\n\t"
                f"Documento reordenado para varredura. Total de paginas: {total_pages}. "
                f"Tamanho do bloco: {self.page_chunk_size}."
            )
            for chunk_start in range(0, total_pages, self.page_chunk_size):
                # Processa blocos curtos para limitar custo de leitura/normalização por iteração.
                chunk_end = min(chunk_start + self.page_chunk_size, total_pages)
                pages_chunk = [document[i] for i in range(chunk_start, chunk_end)]
                should_stop = self._process_pages_chunk(
                    pdf_name=pdf_name,
                    chunk_start=chunk_start,
                    chunk_end=chunk_end,
                    pages_chunk=pages_chunk,
                    labor_claim_state=labor_claim_state,
                )
                if should_stop:
                    break

            remaining_fields = labor_claim_state.missing_fields()
            if remaining_fields:
                logger.warning(
                    f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                    "Extração concluída, mas os seguintes campos não foram encontrados "
                    "(retornados como None): "
                    f"{', '.join(remaining_fields)}."
                )

        return labor_claim_state.to_dict()

    def _process_pages_chunk(
        self,
        pdf_name: str,
        chunk_start: int,
        chunk_end: int,
        pages_chunk: list[Page],
        labor_claim_state: LaborClaimState,
    ) -> bool:
        """
        Processa um bloco de páginas e atualiza o estado acumulado da extração.

        :return: True quando a extração pode ser encerrada antecipadamente.
        """
        normalized_text = self._normalize_pages_chunk_text(pages_chunk)

        # Melhora eficiência tentando apenas campos pendentes com label + valor monetário próximo.
        pending_patterns, pattern_matches = self._get_pending_patterns_and_matches(
            labor_claim_state, normalized_text
        )
        logger.debug(
            f"[LaborClaimCalculationExtractor][_process_pages_chunk] PDF:{pdf_name}\\n\t"
            f"Bloco {chunk_start + 1}-{chunk_end}: "
            f"pendentes={len(pending_patterns)}, labels_com_valor={len(pattern_matches)}."
        )

        if not pending_patterns:
            logger.info(
                f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                f"Extração concluída, interrompi na página {chunk_end}."
            )
            return True

        if not pattern_matches:
            logger.debug(
                f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                f"Bloco de páginas {chunk_start + 1}-{chunk_end} não contém labels pendentes. "
                "Pulando para o próximo bloco."
            )
            return False

        # Tenta extrair primeiro pelo texto HTML/XHTML limpo da página.
        self._extract_fields_from_text(
            normalized_text, labor_claim_state, pattern_matches
        )
        return False

    @staticmethod
    def _normalize_pages_chunk_text(pages_chunk: list[Page]) -> str:
        """
        Concatena texto XHTML normalizado de um bloco de páginas.

        :return: Texto normalizado do bloco, vazio quando não houver conteúdo útil.
        """
        normalized_parts: list[str] = []
        # Carrega paginas e normaliza xhtml
        for page in pages_chunk:
            text = page.get_text("xhtml", sort=True)
            normalized_text = normalize_html_text(text)
            if normalized_text:
                normalized_parts.append(normalized_text)

        return "\n".join(normalized_parts)

    def _extract_fields_from_text(
        self,
        text: str,
        labor_claim_state: LaborClaimState,
        matched_fields: list[FieldName],
    ) -> LaborClaimState:
        """
        Tenta extrair os campos de interesse diretamente do texto normalizado da página.

        :param text: Texto normalizado da página.
        :param labor_claim_state: Estado atual de informações extraídas.
        :param matched_fields: Campos pendentes cujos labels aparecem na página.
        :return: Estado atualizado com os campos extraídos do texto.
        """
        for field_name in list(matched_fields):
            # Só tenta campos pendentes; IRRF com 0 pode ser refinado em páginas seguintes.
            if labor_claim_state.has(field_name) and not self._is_soft_value_for_irrf(
                field_name, getattr(labor_claim_state, field_name)
            ):
                logger.debug(
                    f"[LaborClaimCalculationExtractor][_extract_fields_from_text] "
                    f"Campo '{field_name}' ja definido e nao revisavel neste bloco."
                )
                continue

            extracted = self._extract_field_value_from_text(text, field_name)

            if extracted is not None:
                labor_claim_state.set(field_name, extracted)
                matched_fields.remove(field_name)
                logger.debug(
                    f"[LaborClaimCalculationExtractor][_extract_fields_from_text] "
                    f"Campo '{field_name}' atualizado com valor {extracted}."
                )
            else:
                logger.debug(
                    f"[LaborClaimCalculationExtractor][_extract_fields_from_text] "
                    f"Campo '{field_name}' com label detectado, mas sem valor valido identificado no bloco."
                )

        return labor_claim_state

    def _extract_field_value_from_text(
        self, text: str, field_name: FieldName
    ) -> Decimal | None:
        """
        Extrai o valor de um campo diretamente do texto normalizado da página.
        :param text: Texto normalizado da página.
        :param field_name: Nome do campo desejado.
        :return: Valor extraído como Decimal, ou None.
        """
        if special_extractor := self.special_field_extractors.get(field_name):
            # Encaminha para extratores especializados quando há regra dedicada para o campo.
            return special_extractor(text)

        field_pattern = self.field_pattern_map.get(field_name)
        if not field_pattern:
            return None

        extracted_value = self._extract_money_on_same_line_as_label(text, field_pattern)

        if extracted_value is not None and field_name == "valor_de_irrf":
            return abs(extracted_value)

        return extracted_value

    @staticmethod
    def _has_money_near_label(text: str, label_pattern: str, window: int) -> bool:
        """
        Verifica se existe valor monetário próximo de algum match do label.

        :param text: Texto normalizado do bloco de páginas.
        :param label_pattern: Regex do label do campo.
        :param window: Janela de caracteres à esquerda/direita do label.
        :return: True quando encontrar label com MONEY_RE na vizinhança.
        """
        for label_match in re.finditer(label_pattern, text, re.IGNORECASE):
            left = max(0, label_match.start() - window)
            right = min(len(text), label_match.end() + window)
            snippet = text[left:right]
            if re.search(MONEY_RE, snippet, re.IGNORECASE):
                return True

        return False

    def _get_pending_patterns_and_matches(
        self, labor_claim_state: LaborClaimState, text: str
    ) -> tuple[list[tuple[FieldName, str]], list[FieldName]]:
        """
        Calcula campos pendentes e quais labels desses campos aparecem no texto.
        Evita entrar em métodos com processamento pesado sem necessidade.

        :param labor_claim_state: Estado atual com campos já preenchidos.
        :param text: Texto normalizado do bloco de páginas.
        :return: Tupla com:
            - lista de (campo, regex) ainda pendentes;
            - lista de campos pendentes cujos labels foram encontrados no texto.
        """
        pending_patterns = [
            # Mantém apenas padrões de campos ainda não preenchidos no estado acumulado.
            (field, pattern)
            for field, pattern in self.field_pattern_map.items()
            if not labor_claim_state.has(field)
        ]

        matched_fields = [
            # Reduz falso positivo de label isolado exigindo valor monetário na vizinhança do match.
            field
            for field, pattern in pending_patterns
            if self._has_money_near_label(text, pattern, self.label_money_window)
        ]
        if pending_patterns:
            logger.debug(
                "[LaborClaimCalculationExtractor][_get_pending_patterns_and_matches] "
                f"Campos pendentes: {[field for field, _ in pending_patterns]}; "
                f"matches no bloco: {matched_fields}."
            )
        return pending_patterns, matched_fields

    def _extract_honorarios_demonstrativo_total(self, text: str) -> Decimal | None:
        """
        Extrai honorários do advogado do Demonstrativo de Honorários.

        A extração considera blocos "NOME: HONORARIOS DEVIDOS PELO RECLAMADO"
        e "NOME: HONORARIOS DEVIDOS PELO RECLAMANTE", somando ocorrências de
        honorários advocatícios/sucumbenciais. Isso evita capturar honorários
        periciais ou totais gerais sem discriminação.

        :param text: Texto normalizado da página.
        :return: Soma dos honorários devidos ao advogado, ou None se não encontrado.
        """
        blocks = self._extract_honorarios_due_blocks(text)
        if not blocks:
            # Se não há demonstrativo estruturado, cai para heurísticas de layouts resumidos.
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_honorarios_demonstrativo_total] "
                "Demonstrativo de honorarios nao encontrado; aplicando fallback de layout resumido."
            )
            return self._extract_honorarios_sem_demonstrativo_total(text)

        total = Decimal("0")
        found_value = False
        target_re = re.compile(
            r"HONORARIOS\s+(?:ADVOCATICIOS|(?:DE\s+)?SUCUMBENCIA)", re.IGNORECASE
        )

        for block in blocks:
            # Usa proximidade entre label e valor monetário no texto corrido.
            for target_match in target_re.finditer(block):
                value = self._extract_money_closest_to_span(
                    block, target_match.start(), target_match.end()
                )
                if value is None:
                    continue

                total += value
                found_value = True

        if found_value:
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_honorarios_demonstrativo_total] "
                f"Honorarios extraidos via demonstrativo. Total: {total}."
            )
            return total

        logger.debug(
            "[LaborClaimCalculationExtractor][_extract_honorarios_demonstrativo_total] "
            "Blocos de demonstrativo encontrados sem valor monetario alvo; aplicando fallback."
        )
        return self._extract_honorarios_sem_demonstrativo_total(text)

    def _extract_honorarios_sem_demonstrativo_total(self, text: str) -> Decimal | None:
        """
        Extrai honorários quando o PDF não contém "Demonstrativo de Honorários".

        Cobre layouts resumidos que trazem o valor final em linhas como:
        - HONORARIOS ADVOCATICIOS ...
        - HONORARIOS DE SUCUMBENCIA (10%) ...
        - - da Reclamante ... / - da Reclamada ...

        :param text: Texto normalizado da página.
        :return: Soma dos honorários identificados, ou None.
        """
        primary_total = self._sum_money_closest_to_patterns(
            text,
            [
                r"HONORARIOS\s+ADVOCATICIOS\s+(?:DEVIDOS\s+PELA\s+RECLAMAD[AO]|AO\s+ADVOGADO\s+DO\s+RECTE)",
                r"HONORARIOS\s+DE\s+SUCUMBENCIA\s*\(\s*\d+(?:,\d+)?%\s*\)",
            ],
        )
        if primary_total is not None:
            return primary_total

        # Último fallback para layouts que separam apenas "da Reclamante/Reclamada".
        return self._sum_money_closest_to_patterns(
            text, [r"-\s*DA\s+RECLAMANTE", r"-\s*DA\s+RECLAMADA"]
        )

    def _sum_money_closest_to_patterns(
        self, text: str, label_patterns: list[str]
    ) -> Decimal | None:
        """
        Soma valores monetários mais próximos de labels em cada linha.

        :param text: Texto normalizado da página.
        :param label_patterns: Regex de labels que apontam para valores alvo.
        :return: Soma dos valores encontrados, ou None.
        """
        total = Decimal("0")
        found_any = False

        compiled_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in label_patterns
        ]

        for line in text.splitlines():
            if not line:
                continue

            # Em cada linha, soma o valor mais próximo de cada ocorrência de label alvo.
            for pattern in compiled_patterns:
                for label_match in pattern.finditer(line):
                    value = self._extract_money_on_line_for_label_span(
                        line, label_match.start(), label_match.end()
                    )
                    if value is None:
                        continue
                    total += value
                    found_any = True

        return total if found_any else None

    @staticmethod
    def _extract_money_on_line_for_label_span(
        line: str, span_start: int, span_end: int
    ) -> Decimal | None:
        """
        Extrai o valor monetário mais próximo do label na linha, priorizando valores à direita.

        :param line: Linha já sanitizada para busca.
        :param span_start: Início do span do label.
        :param span_end: Fim do span do label.
        :return: Valor monetário mais próximo, ou None.
        """
        money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
        if not money_matches:
            return None

        after_matches = [match for match in money_matches if match.start() >= span_end]
        if after_matches:
            # Regra principal: prioriza valores à direita do label.
            closest_after = min(
                after_matches, key=lambda match: match.start() - span_end
            )
            return to_decimal(closest_after.group(0))

        before_matches = [match for match in money_matches if match.end() <= span_start]
        if not before_matches:
            return None

        # Sem candidato à direita, usa o valor anterior ao label.
        closest_before = min(before_matches, key=lambda match: span_start - match.end())
        return to_decimal(closest_before.group(0))

    def _extract_contribuicao_social_value(self, text: str) -> Decimal | None:
        """
        Extrai contribuição social via total explícito ou soma INSS reclamante+reclamada.

        :param text: Texto normalizado da página.
        :return: Valor da contribuição social, ou None.
        """
        direct_match = self._extract_money_on_same_line_as_label(
            text,
            (
                r"(?:CONTRIBUICAO\s+SOCIAL\s+SOBRE\s+SALARIOS\s+DEVID[OA]S?"
                r"|TOTAL\s+DA\s+CONTRIBUICAO\s+PREVIDENCIARIA"
                r"|INSS\s+COTA-EMPREGADOR"
                r"|INSS\s+PARTE\s+DA\s+RECLAMAD[AO])"
            ),
        )
        if direct_match is not None:
            # Normaliza sinal para retornar contribuição sempre positiva.
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_contribuicao_social_value] "
                f"Contribuicao social obtida por label direto: {abs(direct_match)}."
            )
            return abs(direct_match)

        # Quando não há total explícito, compõe a contribuição pela soma das parcelas.
        logger.debug(
            "[LaborClaimCalculationExtractor][_extract_contribuicao_social_value] "
            "Label direto indisponivel; tentando composicao INSS reclamante + reclamada."
        )
        return self._extract_inss_reclamante_reclamada_sum(text)

    def _extract_inss_reclamante_reclamada_sum(self, text: str) -> Decimal | None:
        """
        Soma INSS da parte reclamante e da parte reclamada quando aparecem separados.

        :param text: Texto normalizado da página.
        :return: Soma das parcelas encontradas, ou None.
        """
        inss_reclamante = self._extract_money_on_same_line_as_label(
            text, r"INSS\s+(?:DO|DA|PARTE\s+DO|PARTE\s+DA)\s+RECLAMANT[EA]"
        )
        inss_reclamada = self._extract_money_on_same_line_as_label(
            text, r"INSS\s+(?:DO|DA|PARTE\s+DO|PARTE\s+DA)\s+RECLAMAD[AO]"
        )

        if inss_reclamada is None:
            # Exige ao menos a parcela da reclamada para considerar o campo confiável.
            return None

        return abs(inss_reclamante or Decimal("0")) + abs(
            inss_reclamada or Decimal("0")
        )

    @staticmethod
    def _extract_honorarios_due_blocks(text: str) -> list[str]:
        """
        Recorta blocos do Demonstrativo de Honorários devidos pelo reclamado/reclamante.

        O recorte é tolerante a texto corrido (xhtml normalizado), evitando
        depender exclusivamente de quebras de linha.

        :param text: Texto normalizado da página.
        :return: Lista de blocos do demonstrativo de honorários.
        """
        if not re.search(r"DEMONSTRATIVO\s+DE\s+HONORARIOS", text, re.IGNORECASE):
            return []

        demonstrativo_re = re.compile(
            r"DEMONSTRATIVO\s+DE\s+HONORARIOS(?P<body>.*?)(?=DEMONSTRATIVO\s+DE\s+|$)",
            re.IGNORECASE | re.DOTALL,
        )
        due_block_re = re.compile(
            r"NOME\s*:\s*HONORARIOS\s+DEVIDOS\s+PELO\s+(?:RECLAMADO|RECLAMANTE)(?P<body>.*?)(?=NOME\s*:|$)",
            re.IGNORECASE | re.DOTALL,
        )

        blocks: list[str] = []
        for demonstrativo_match in demonstrativo_re.finditer(text):
            demonstrativo_body = demonstrativo_match.group("body")
            # Dentro de cada demonstrativo, separa blocos por parte (reclamado/reclamante).
            for block_match in due_block_re.finditer(demonstrativo_body):
                blocks.append(block_match.group("body"))

        return blocks

    @staticmethod
    def _extract_money_closest_to_span(
        text: str, span_start: int, span_end: int
    ) -> Decimal | None:
        """
        Retorna o valor monetário mais próximo de um intervalo de texto.

        Útil para linhas longas em que o label e o valor não estão próximos.

        :param text: Texto no qual será feita a busca por valores monetários.
        :param span_start: Índice inicial do label no texto.
        :param span_end: Índice final do label no texto.
        :return: Valor monetário mais próximo do span, ou None se não houver candidato.
        """
        money_matches = list(re.finditer(MONEY_RE, text, re.IGNORECASE))
        if not money_matches:
            return None

        before_matches = [match for match in money_matches if match.end() <= span_start]
        if before_matches:
            # Neste helper, prioriza valores imediatamente antes do span (comportamento legado).
            closest_before = min(
                before_matches, key=lambda match: span_start - match.end()
            )
            return to_decimal(closest_before.group(0))

        after_matches = [match for match in money_matches if match.start() >= span_end]
        if not after_matches:
            return None

        closest_after = min(after_matches, key=lambda match: match.start() - span_end)
        return to_decimal(closest_after.group(0))

    def _extract_fgts_field_value(self, text: str) -> Decimal | None:
        """
        Extrai o valor final de FGTS.

        A ordem de prioridade é:
        1. linha cujo label seja exatamente 'FGTS';
        2. sequência "TOTAL DEVIDO AO AUTOR" quando há evidência de FGTS/JUROS FGTS;
        3. total consolidado do Anexo IX.

        Isso evita capturar linhas intermediárias como:
        - FGTS 8% 6.886,94 15.650,90 8.763,96
        - MULTA SOBRE FGTS 40% 2.607,33 5.970,54 3.363,21
        - DIFERENCA DE FGTS DO CONTRATO 0,00 0,00 0,00 2.259,62 0,00 2.259,62

        :param text: Texto normalizado da página.
        :return: Valor de FGTS convertido para Decimal, ou None se não for encontrado.
        """
        if (exact_fgts_value := self._extract_exact_fgts_line_value(text)) is not None:
            # Melhor caso: label exato "FGTS" evita confusão com linhas intermediárias.
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_fgts_field_value] "
                f"FGTS extraido por label exato: {exact_fgts_value}."
            )
            return exact_fgts_value

        if (
            total_devido_ao_autor_fgts := self._extract_fgts_from_total_devido_ao_autor_sequence(
                text
            )
        ) is not None:
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_fgts_field_value] "
                "FGTS extraido por sequencia 'TOTAL DEVIDO AO AUTOR'."
            )
            return total_devido_ao_autor_fgts

        # Último fallback para demonstrativos consolidados (Anexo IX).
        logger.debug(
            "[LaborClaimCalculationExtractor][_extract_fgts_field_value] "
            "FGTS nao identificado nos criterios primarios; tentando fallback Anexo IX."
        )
        return self._extract_fgts_from_anexo_ix_total(text)

    @staticmethod
    def _extract_fgts_from_total_devido_ao_autor_sequence(text: str) -> Decimal | None:
        """
        Extrai FGTS de sequências textuais com "TOTAL DEVIDO AO AUTOR".

        Alguns layouts trazem um cabeçalho com FGTS/JUROS FGTS e, na linha
        de total, uma sequência de valores monetários. Nesses casos, usa o
        4o valor monetário da linha de total como candidato a FGTS.

        :param text: Texto normalizado da página.
        :return: Valor de FGTS extraído da sequência, ou None.
        """
        if not re.search(r"\bFGTS\b", text, re.IGNORECASE):
            return None

        if not re.search(r"\bJUROS\s+FGTS\b", text, re.IGNORECASE):
            return None

        total_devido_ao_autor_re = re.compile(
            r"\bTOTAL\s+DEVIDO\s+AO\s+AUTOR\b", re.IGNORECASE
        )

        for line in text.splitlines():
            if not total_devido_ao_autor_re.search(line):
                continue

            money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
            # todo hardcoded, somente esse caso dessa maneira no universo de pdf's, necessario generalizar?
            if len(money_matches) < 6:
                continue

            return to_decimal(money_matches[3].group(0))

        return None

    def _extract_irrf_field_value(self, text: str) -> Decimal | None:
        """
        Extrai IRRF priorizando labels explícitos e o total do Demonstrativo de Imposto de Renda.

        Ordem de prioridade:
        1. Labels diretos de alta confiança (VALOR TOTAL DO IRRF, IRRF DO/DEVIDO PELO RECLAMANTE).
        2. Campo "TOTAL DEVIDO" dentro do bloco "Demonstrativo de Imposto de Renda".
        3. Labels fallback na mesma linha (IMPOSTO DE RENDA A RECOLHER e IMPOSTO DE RENDA).

        :param text: Texto normalizado da página.
        :return: Valor de IRRF em Decimal (sempre absoluto), ou None.
        """

        def resolve_candidate(value: Decimal | None) -> Decimal | None:
            # Padroniza sinal e controla quando zero é aceitável como valor final.
            if value is None:
                return None

            return abs(value)

        high_confidence_patterns = (
            r"VALOR\s+TOTAL\s+DO\s+IRRF",
            r"IRRF\s+DEVIDO\s+PELO\s+RECLAMANTE",
            r"IRRF\s+DO\s+RECLAMANTE",
        )

        for label_pattern in high_confidence_patterns:
            value = resolve_candidate(
                self._extract_money_on_same_line_as_label(text, label_pattern)
            )
            if value is not None:
                logger.debug(
                    "[LaborClaimCalculationExtractor][_extract_irrf_field_value] "
                    f"IRRF extraido por label de alta confianca '{label_pattern}': {value}."
                )
                return value

        # Em alguns layouts o único valor confiável está no bloco do demonstrativo.
        demonstrativo_total = resolve_candidate(
            self._extract_irrf_demonstrativo_total_devido(text)
        )
        if demonstrativo_total is not None:
            logger.debug(
                "[LaborClaimCalculationExtractor][_extract_irrf_field_value] "
                f"IRRF extraido do demonstrativo de imposto de renda: {demonstrativo_total}."
            )
            return demonstrativo_total

        fallback_same_line_patterns = (
            r"IMPOSTO\s+DE\s+RENDA\s+A\s+RECOLHER",
            r"IMPOSTO\s+DE\s+RENDA",
        )

        for label_pattern in fallback_same_line_patterns:
            value = resolve_candidate(
                self._extract_money_on_same_line_as_label(text, label_pattern)
            )
            if value is not None:
                logger.debug(
                    "[LaborClaimCalculationExtractor][_extract_irrf_field_value] "
                    f"IRRF extraido por label fallback '{label_pattern}': {value}."
                )
                return value

        logger.debug(
            "[LaborClaimCalculationExtractor][_extract_irrf_field_value] "
            "IRRF nao identificado neste bloco de texto."
        )
        return None

    def _extract_irrf_demonstrativo_total_devido(self, text: str) -> Decimal | None:
        """
        Extrai IRRF pelo "TOTAL DEVIDO" dentro do bloco Demonstrativo de Imposto de Renda.

        :param text: Texto normalizado da página.
        :return: Valor do total devido do demonstrativo, ou None.
        """
        demonstrativo_block_re = re.compile(
            r"DEMONSTRATIVO\s+DE\s+IMPOSTO\s+DE\s+RENDA(?P<body>.*?)(?=DEMONSTRATIVO\s+DE\s+|$)",
            re.IGNORECASE | re.DOTALL,
        )
        total_devido_re = re.compile(r"TOTAL\s+DEVIDO", re.IGNORECASE)

        for demonstrativo_match in demonstrativo_block_re.finditer(text):
            demonstrativo_body = demonstrativo_match.group("body")
            # Busca "TOTAL DEVIDO" apenas dentro do escopo do demonstrativo de IR.
            for total_devido_match in total_devido_re.finditer(demonstrativo_body):
                value = self._extract_money_closest_to_span(
                    demonstrativo_body,
                    total_devido_match.start(),
                    total_devido_match.end(),
                )
                if value is not None:
                    return value

        return None

    def _extract_fgts_from_anexo_ix_total(self, text: str) -> Decimal | None:
        """
        Extrai FGTS do resumo "Anexo IX - FGTS + Multa 40%" quando houver total final.

        Exemplo:
        - Selic Simples 15,93% 68.596,52 Total 499.208,72

        :param text: Texto normalizado da página.
        :return: Valor total do anexo IX, ou None.
        """
        if not re.search(r"ANEXO\s+IX", text, re.IGNORECASE):
            return None

        if not re.search(self.field_pattern_map["valor_do_fgts"], text, re.IGNORECASE):
            return None

        for line in text.splitlines():
            if not re.search(r"ANEXO\s+IX", line, re.IGNORECASE):
                continue

            # Em linhas de resumo, escolhe o último "TOTAL" monetário como valor final do anexo.
            total_matches = list(
                re.finditer(rf"\bTOTAL\b\s*({MONEY_RE})", line, re.IGNORECASE)
            )
            if total_matches:
                return to_decimal(total_matches[-1].group(1))

        multiline_match = re.search(
            rf"ANEXO\s+IX.*?FGTS.*?SELIC\s+SIMPLES.*?\bTOTAL\b\s*({MONEY_RE})",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if multiline_match:
            return to_decimal(multiline_match.group(1))

        return None

    def _extract_exact_fgts_line_value(self, text: str) -> Decimal | None:
        """
        Extrai o valor de linhas cujo label seja exatamente 'FGTS'.

        Aceita casos como:
        - FGTS 21.621,44
        - 21.621,44 FGTS
        - | FGTS | 21.621,44 |

        Descarta casos como:
        - FGTS 8% 6.886,94
        - MULTA SOBRE FGTS 40% 2.607,33

        :param text: Texto normalizado da página.
        :return: Valor de FGTS convertido para Decimal, ou None se não for encontrado.
        """
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            money_matches = list(re.finditer(MONEY_RE, line, re.IGNORECASE))
            if not money_matches:
                continue

            label_without_values = re.sub(
                MONEY_RE, " ", line, flags=re.IGNORECASE
            ).strip()

            if not re.fullmatch(
                self.field_pattern_map["valor_do_fgts"],
                label_without_values,
                flags=re.IGNORECASE,
            ):
                continue

            return to_decimal(money_matches[0].group(0))

        return None

    @staticmethod
    def _extract_money_on_same_line_as_label(
        text: str, field_pattern: str
    ) -> Decimal | None:
        """
        Extrai um valor monetário que esteja na mesma linha do label.

        A busca aceita valor depois ou antes do label na mesma linha, mas descarta
        candidatos em outras linhas para evitar capturar valores de campos vizinhos.

        :param text: Texto normalizado da página.
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
                # Ranqueia candidatos pela menor distância até o label na mesma linha, com preferencia da direita
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
    import time

    data_path = Path(__file__).parents[2] / "data" / "Documentos"

    extractor = LaborClaimCalculationExtractor()

    def run_all_pdfs():
        pdf_files = list(data_path.glob("*.pdf"))
        for pdf_file in pdf_files:
            # Permite rodar lote pulando casos específicos durante depuração.
            print(extractor.extract(pdf_file))

    start = time.time()
    print(extractor.extract(data_path / "1001298-45.2023.5.02.0059 - Reclamada.pdf"))
    end = time.time()
    print(f"Tempo de extração: {end - start:.2f} segundos")

    run_all_pdfs()
