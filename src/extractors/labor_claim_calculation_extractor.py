import re
import html
import fitz
import unicodedata

from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

from utils.general_utils import get_logger

logger = get_logger(__name__)

MONEY_RE = r"\(?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}\s*\)?"


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
        - converte quebras/fechamentos de blocos HTML em linhas
        - remove tags restantes
        - remove acentos (ASCII fold)
        - junta linhas por " | " para preservar separação sem perder legibilidade
        :param html_text: O texto HTML bruto extraído da página.
        :return: O texto normalizado
        """
        decoded_html = html.unescape(html_text or "")
        with_line_breaks = re.sub(r"(?i)<br\s*/?>", "\n", decoded_html)
        with_line_breaks = re.sub(
            r"(?i)</(?:p|div|tr|li|h[1-6]|td|th)>", "\n", with_line_breaks
        )

        no_tags = re.sub(r"<[^>]+>", " ", with_line_breaks)
        normalized = self._normalize_text(no_tags)

        lines = [re.sub(r"\s+", " ", line).strip() for line in normalized.splitlines()]
        lines = [line for line in lines if line]
        return " | ".join(lines)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Métod0 auxiliar para normalizar texto, removendo acentos e caracteres especiais e padronizando espaços.
        :param text: O texto a ser normalizado.
        :return: O texto normalizado sem acentos e com espaços ajustados.
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
        Verifica quais campos ainda estão pendentes e quais labels aparecem no texto da página.
        :param labor_claim_info: O dicionário atual de informações extraídas.
        :param text: O texto normalizado da página atual.
        :return: Tupla com (campos pendentes com regex, nomes dos campos cujos labels aparecem no texto).
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

    @staticmethod
    def _set_field_value(
        labor_claim_info: LaborClaimInfo, field_name: str, value: Decimal
    ) -> None:
        """
        Atribui um valor a um campo específico no dicionário de informações extraídas,
         usando cast para evitar warnings de chave dinâmica na IDE em TypedDict.
        :param labor_claim_info: O dicionário de informações extraídas onde o valor deve ser atribuído.
        :param field_name: O nome do campo a ser atualizado, correspondente a uma chave do field_pattern_map.
        :param value: O valor Decimal a ser atribuído ao campo especificado.
        :return: None
        """
        cast(dict[str, Decimal], labor_claim_info)[field_name] = value

    def extract(self, pdf_path: str | Path) -> LaborClaimInfo:
        """
        Extrai informações contábeis navegando pelas páginas do PDF e processando apenas o HTML normalizado.
        :param pdf_path: Caminho para o arquivo PDF a ser processado.
        :return: Dicionário com os campos extraídos.
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
        pending_patterns: list[tuple[str, str]] = list(self.field_pattern_map.items())

        logger.info(
            f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\tPercorrendo paginas em busca"
            " dos campos de interesse."
        )
        with fitz.open(pdf_path) as document:
            for page_index, page in enumerate(document, start=1):
                page_html = page.get_text("xhtml", sort=True)
                normalized_html = self._normalize_html_text(page_html)

                if not normalized_html:
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Página {page_index} do sem texto extraído. "
                        "Pulando para a próxima página."
                    )
                    continue

                # melhora eficiência verificando labels pendentes no texto da página
                pending_patterns, pattern_matches = (
                    self._get_pending_patterns_and_matches(
                        labor_claim_info, normalized_html
                    )
                )

                if not pending_patterns:
                    logger.info(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Extração concluída, interrompi na página {page_index}."
                    )
                    break

                # verifica se algum dos padrões pendentes aparece no texto da página
                if not pattern_matches:
                    logger.debug(
                        f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                        f"Página {page_index} não contém labels pendentes. "
                        "Pulando para a próxima página."
                    )
                    continue

                self.extract_fields(normalized_html, labor_claim_info, pattern_matches)
            if pending_patterns:
                for field_name, _ in pending_patterns:
                    self._set_field_value(labor_claim_info, field_name, Decimal(0))
                logger.warning(
                    f"[LaborClaimCalculationExtractor][extract] PDF:{pdf_name}\n\t"
                    f"Extração concluída, mas os seguintes campos não foram encontrados: "
                    f"{', '.join(field for field, _ in pending_patterns)}"
                )

        return labor_claim_info

    def extract_fields(
        self,
        normalized_html: str,
        labor_claim_info: LaborClaimInfo,
        matched_fields: list[str],
    ) -> LaborClaimInfo:
        """
        Extrai os campos de interesse exclusivamente a partir do HTML normalizado da página.
        :param normalized_html: O texto HTML normalizado da página.
        :param labor_claim_info: O dicionário atual de informações extraídas.
        :param matched_fields: Campos pendentes cujos padrões foram encontrados no texto da página.
        :return: O dicionário atualizado de informações extraídas.
        """
        for field_name in matched_fields:
            extracted_field = self.extract_field_value_from_html(
                normalized_html, field_name
            )
            if (
                extracted_field is None
            ):  # is None explicito para diferenciar de valores Decimal(0)
                continue
            self._set_field_value(labor_claim_info, field_name, extracted_field)

        return labor_claim_info

    def extract_field_value_from_html(
        self, normalized_html: str, field_name: str
    ) -> Decimal | None:
        """
        Extrai o valor de um campo diretamente do HTML normalizado da página.
        :param normalized_html: HTML normalizado da página.
        :param field_name: Nome do campo, correspondente a uma chave do field_pattern_map.
        :return: Valor convertido para Decimal, ou None quando não encontrado.
        """
        field_pattern = self.field_pattern_map.get(field_name)
        if not field_pattern:
            return None

        html_as_lines = normalized_html.replace("|", "\n")
        clean_text = re.sub(r"[ \t]+", " ", html_as_lines)

        value_after_label = self._extract_field_value_after_label(
            clean_text, field_pattern
        )
        if value_after_label is not None:
            return value_after_label

        if field_name == "total_devido_pelo_reclamado":
            return self._extract_field_value_before_label(clean_text, field_pattern)

        return None

    def _extract_field_value_after_label(
        self, text: str, field_pattern: str, window_size: int = 250
    ) -> Decimal | None:
        """
        Procura um label no texto e retorna o primeiro valor monetário encontrado logo após ele.
        :param text: Texto normalizado de entrada.
        :param field_pattern: Regex do label correspondente ao campo.
        :param window_size: Quantidade de caracteres analisados após cada ocorrência do label.
        :return: Valor convertido para Decimal, ou None quando não encontrado.
        """
        label_anywhere_re = re.compile(field_pattern, re.IGNORECASE)
        for label_match in label_anywhere_re.finditer(text):
            window = text[label_match.end() : label_match.end() + window_size]
            value_match = re.search(MONEY_RE, window, re.IGNORECASE)
            if value_match:
                return self._to_decimal(value_match.group(0))
        return None

    def _extract_field_value_before_label(
        self, text: str, field_pattern: str, window_size: int = 250
    ) -> Decimal | None:
        """
        Procura um label no texto e retorna o valor monetário mais próximo encontrado imediatamente antes dele.
        :param text: Texto normalizado de entrada.
        :param field_pattern: Regex do label correspondente ao campo.
        :param window_size: Quantidade de caracteres analisados antes de cada ocorrência do label.
        :return: Valor convertido para Decimal, ou None quando não encontrado.
        """
        label_anywhere_re = re.compile(field_pattern, re.IGNORECASE)
        for label_match in label_anywhere_re.finditer(text):
            window_start = max(0, label_match.start() - window_size)
            window = text[window_start : label_match.start()]
            monetary_matches = list(re.finditer(MONEY_RE, window, re.IGNORECASE))
            if not monetary_matches:
                continue
            return self._to_decimal(monetary_matches[-1].group(0))
        return None

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
