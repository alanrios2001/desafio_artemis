# Desafio Artemis

> O objetivo do projeto é ter uma funcionalidade para extrair informações contábeis processuais de arquivos PDF.

## Estrutura do projeto
```text
.
├── config.py / settings.toml
│   └── Configuração central com Dynaconf
├── escavai_dataset/
│   └── Ferramentas de stats, dedup e conversão de datasets
├── src/
│   ├── extractors/
│       ├──labor_claim_calculation_extractor.py / ...
│           └── Extração de dados a partir de PDFs, com foco em cálculos trabalhistas
│   └── main.py
├── tests/
│   └── test_labor_claim_calculation_extractor.py
│   ├── data/
│       └──Casos de teste para os testes unitários separado por pasta.
└── Raiz do projeto com readme, pastas de configuração, pyproject.toml, etc.
```

Stack principal: Python + Poetry + Dynaconf + pymupdf

## Como Instalar o projeto

```bash
# Install Poetry
# Ubuntu/Debian
curl -sSL https://install.python-poetry.org | python3 -

# Windows
pip install poetry

# Clone o repositório
git clone https://github.com/alanrios2001/desafio_artemis.git

# Entre na pasta do projeto
cd desafio_artemis

# Setup
poetry install
```

## Tecnologias e Soluções adotadas.

- **Python**: Linguagem principal para desenvolvimento.
- **Poetry**: Gerenciamento de dependências e ambiente virtual.
- **Dynaconf**: Configuração centralizada e flexível.
- **pymupdf**: Biblioteca para extração de texto e dados de arquivos PDF

O projeto foi estrutuado para ser modular e escalável.
Específicamente na classe de extração de cálculos trabalhistas, foi utilizado o pymupdf para ler o conteúdo dos PDFs,
técnicas com modelos para OCR foram descartadas, pois todo o úniverso de PDF's a serem processados possuem
texto selecionável.

Extrair tabelas do pdf é um pouco mais custoso utilizando o método 
".find_tables()" dos objetos de página do pymudf + .to_markdown(), então foi feito um fluxo em que primeiro se tenta
extrair as informações a partir do método .get_text("xhtml), decodificando o xhtml com o html.unescape,
normalizando o xhtml decodificado removendo espaços extra, acentuações e preservando quebras de linhas. E caso não
seja possível extrair as informações a partir do xhtml, há um fallback utilizando a extração de tabelas
com o método .find_tables() + .to_markdown().

A extrutura de dados utilizada para armazenar oc campos extraídos foi um DataClass, por permitir criar
métodos de validação, inserção, verificação de campos preenchidos, evitando erros de tipagem. Nesse dataclass, foi
criado um método .to_dict() para converter o objeto em um dicionário, requisito de retorno do projeto.


Fluxo genérico:

O PDF é iterado página a página, tem seu texto extraído em xhtml decodificado e normalizado.
O Texto passar por um método para identificar os rótulos e valores ali presentes, que retorna os campos pendentes
de extração e os campos pendentes que tiveram o regex com match na página.
Caso existam campos pendentes com regex match, é feita uma lógica de extração a partir do xhtml, é tentado
realizar as extrações somentes do campo com regex match, para evitar tentativas de extração desnecessárias,
e cair no fallback de tabelas sem motivo, deixando o processo mais otimizado.

Seguindo o fluxo de extração a partir do xhtml:
Como a natureza dos dados que foram requisitados para extração geralmente aparecem em tabelas e caso esteja
fora de uma tabela, tem um rótulo proximo a ele, foi feita uma lógica de identificação de padrões de rótulos e valores,
utilizando regex para identificar os rótulos e os valores próximos a eles. em que em um range de distancia de chars
a partir do rótulo, é calculada a distancia de chars entre o rótulo e os valores próximos a ele, 
e o valor mais próximo é selecionado como o valor do rótulo, o desempate é o rótulo à direita ou na mesma
linha do rótulo.

Após a primeira etapa, é verificado se os campos de match ainda estão pendentes, caso estejam, o fluxo vai para fallback
e tenta extrair os campos a partir das tabelas, utilizando o método .find_tables() do pymupdf, e convertendo as tabelas
em markdown com o método .to_markdown(), e utilizando regex para identificar os campos a partir do markdown da tabela,
processando linha a linha, selecionando os campos que possuem match com os campos pendentes, e selecionando 
o valor a direita do campo.

Caso o campo não tenha sido extraído de nenhuma forma, o campo passa a ter o valor None para
diferenciar de quando o valor do campo é 0.


Regras específicas:

Existem regras específicas para 3 campos, que possuem casos de borda mais complexos,
que são: "liquido_devido_ao_advogado", "valor_do_fgts", "contribuicao_social_sobre_salarios_devido", "valor_do_irrf".

"liquido_devido_ao_advogado": Nesse caso, a descrição dos honorarios na maioria dos casos aparece no Demonstrativo
de honorários, que engloba varios tipos de honorários, como o honorário advocatício/sucubencial, pericial, etc. O valor
total desse demonstrativo, engloba todos esses tipos de honorários, então é necessario identificar os honorários
referentes somente ao advogado, e somar. Além disso, tem honorários devidos pelo Reclamado e Reclamante,
em tabelas diferentes, então esse campo é tratado de forma mais específica. Os outros casos são referentes a diferentes
formatações de tabelas e texto, mas o valor está aparentemente somado.

"valor_do_fgts": O valor do fgts tem labels que possuem o valor já corretamente somado e descontado, e labels de linhas
de tabelas com colunas e varios valores, que são mais custosos de extrair, além de ter FGTS em varias partes do PDF,
o que pode levar a falsos positivos. O que a gente quer, é o campo "FGTS" presente na maior parte dos PDF's
(PJe-Calc Cidadão). Porém existem casos do valor do FGTS já somado estar em tabelas compostas de uma coluna,
e o campo correto ter o label "Total"  (Laudo Pericial), e um caso em que o valor do FGTS está em uma tabela
composta com varias colunas e a linha com o valor total ter o label "TOTAL DEVIDO AO AUTOR".

"contribuição_social_sobre_salarios_devido": Esse campo possui casos de borda mais simples, mas vale a pena
separa-lo. Os valores sempre estarão na mesma linha do rótulo, mas existem casos em que o valor aparece
separado entre o reclamado e a reclamada, necessitando de uma soma.

"valor_do_irrf": O valor do IRRF tem casos de borda mais simples, mas existem casos em que o valor aparecem
em blocos de Demonstrativo, e diferentes labels.



