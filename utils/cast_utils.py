import re
from decimal import Decimal

from utils.general_utils import get_logger

logger = get_logger(__name__)


def to_decimal(raw_value: str) -> Decimal | None:
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
        logger.error("[_to_decimal] " f"Erro ao converter valor para Decimal: {e}")
        return None
