import html
import re

import unicodedata


def normalize_text(text: str) -> str:
    """
    Função para normalizar texto, removendo acentos e caracteres especiais, e colapsando espaços.
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


def normalize_html_text(html_text: str) -> str:
    """
    Normaliza HTML da página para regex:
    - decodifica entidades HTML
    - converte quebras/fechamentos de blocos HTML em linhas
    - remove tags restantes
    - remove acentos
    - preserva linhas para facilitar a busca por rótulo + valor
    :param html_text: O texto HTML bruto extraído da página.
    :return: O texto normalizado.
    """
    decoded_html = html.unescape(html_text or "")
    with_line_breaks = re.sub(r"(?i)<br\s*/?>", "\n", decoded_html)
    with_line_breaks = re.sub(
        r"(?i)</(?:p|div|tr|li|h[1-6]|td|th)>", "\n", with_line_breaks
    )

    no_tags = re.sub(r"<[^>]+>", " ", with_line_breaks)
    normalized = normalize_text(no_tags)

    lines = [re.sub(r"\s+", " ", line).strip() for line in normalized.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)
