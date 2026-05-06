# Desafio Artemis

> O objetivo do projeto é extrair informações contábeis processuais de arquivos PDF.

## Estrutura do projeto

```text
.
├── config.py / settings.toml
│   └── Configuração central com Dynaconf
├── src/
│   ├── extractors/
│   │   └── labor_claim_calculation_extractor.py / ...
│   │       └── Extração de dados a partir de PDFs, com foco em cálculos trabalhistas
│   └── main.py
├── tests/
│   ├── test_labor_claim_calculation_extractor.py
│   └── data/
│       └── Casos de teste para testes unitários, separados por pasta
└── Raiz do projeto com README, pastas de configuração, pyproject.toml etc.
```

Stack principal: Python + Poetry + Dynaconf + pymupdf.

## Como instalar o projeto

```bash
# Instalar Poetry
# Ubuntu/Debian
curl -sSL https://install.python-poetry.org | python3 -

# Windows
pip install poetry

# Clonar o repositório
git clone https://github.com/alanrios2001/desafio_artemis.git

# Entrar na pasta do projeto
cd desafio_artemis

# Instalar dependências
poetry install
```

## Como executar o projeto

```bash
# Ativar o ambiente virtual do Poetry
poetry shell

# Executar o script principal
poetry run python src/main.py
```

## Tecnologias e soluções adotadas

- **Python**: linguagem principal para desenvolvimento.
- **Poetry**: gerenciamento de dependências e ambiente virtual.
- **Dynaconf**: configuração centralizada e flexível.
- **pymupdf**: extração de texto e dados de arquivos PDF.
- **Regex + heurística de proximidade**: identificação de rótulos e seleção do valor monetário mais próximo (com prioridade para a direita/mesma linha).
- **asyncio**: pipeline assíncrono para produzir e consumir PDFs em paralelo no processamento em lote.
- **dataclass**: estrutura de estado da extração com tipagem explícita e serialização final via `to_dict()`.

O projeto foi estruturado para ser modular e escalável.
Na classe de extração de cálculos trabalhistas, o `pymupdf` é utilizado para ler o conteúdo dos PDFs.
Técnicas com OCR foram descartadas porque o universo de PDFs processados possui texto selecionável.

Antes de tentar extração de valores, o fluxo faz uma verificação robusta no texto normalizado: além do match do label por regex, exige indício de valor monetário próximo ao label (janela local de contexto). Com isso, o processamento segue totalmente pela trilha textual via `get_text("xhtml")`, focando apenas candidatos com maior chance de sucesso.

A extração textual é guiada por regex e heurísticas de proximidade entre rótulo e valor, reduzindo falsos positivos e priorizando candidatos mais confiáveis por contexto de linha. Mesmo sem essa pré-validação, a extração ainda pode acertar os campos; porém, tende a fazer verificações desnecessárias dentro do fluxo, com custo maior de processamento.

No processamento em lote, há uma orquestração assíncrona com `asyncio`, usando fila de PDFs e workers consumidores para manter boa vazão.

A estrutura de dados para armazenar os campos extraídos é um `dataclass`, que mantém o estado acumulado da extração com tipagem mais segura e converte o resultado final para dicionário via `to_dict()`, atendendo ao requisito de retorno do projeto.

## Fluxo genérico

1. O PDF é iterado em chunks para evitar tabelas quebradas e reduzir erros na extração dos valores.
2. O texto é extraído em XHTML, decodificado e normalizado.
3. O conteúdo passa por uma pré-validação que identifica rótulos pendentes e confirma se há valor monetário próximo, retornando:
   - campos ainda pendentes;
   - campos pendentes considerados candidatos reais para extração no bloco.
4. Quando há candidato válido, a extração tenta focar somente nesses campos para evitar tentativas desnecessárias.

### Extração via XHTML

Como os dados geralmente aparecem em tabelas (ou com rótulos próximos), foi aplicada uma estratégia de proximidade entre rótulo e valor:

- usa regex para identificar rótulos e valores próximos;
- calcula a distância em caracteres entre o rótulo e os candidatos a valor;
- escolhe o valor mais próximo;
- em caso de empate, prioriza o valor à direita ou na mesma linha do rótulo.

Caso o campo não seja extraído pela estratégia textual, ele recebe valor `None` para diferenciar de campos cujo valor real é `0`.

## Regras específicas

Existem regras específicas para campos com casos de borda mais complexos:

- `liquido_devido_ao_advogado`
- `valor_do_fgts`
- `contribuicao_social_sobre_salarios_devido`
- `valor_do_irrf`

### `liquido_devido_ao_advogado`

Na maioria dos casos, a descrição aparece no Demonstrativo de Honorários, que engloba tipos diferentes (advocatício/sucumbencial, pericial etc.). Como o total mistura naturezas distintas, é necessário identificar e somar apenas os honorários referentes ao advogado.

Também há honorários devidos pelo reclamado e pelo reclamante em tabelas diferentes, então esse campo recebe tratamento mais específico. Há ainda variações de formatação de tabela e texto, embora o valor final normalmente esteja somado.

### `valor_do_fgts`

Esse campo possui labels com valor já consolidado e também labels em linhas de tabelas com várias colunas, mais custosas de extrair. Além disso, FGTS pode aparecer em diferentes trechos do PDF, aumentando risco de falso positivo.

O alvo principal é o campo `FGTS` comum nos PDFs do PJe-Calc Cidadão. Porém, há casos em que:

- o valor consolidado aparece em tabela de coluna única;
- o label correto é `Total` (Laudo Pericial);
- o total está em tabela com múltiplas colunas sob o label `TOTAL DEVIDO AO AUTOR`.

### `contribuicao_social_sobre_salarios_devido`

Possui casos de borda mais simples, mas vale separar a regra. Os valores costumam estar na mesma linha do rótulo; em alguns casos, o valor vem dividido entre reclamado e reclamada, exigindo soma.

### `valor_do_irrf`

Também tem casos de borda mais simples, mas há ocorrências em blocos de demonstrativo e com labels diferentes.



