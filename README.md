# PesquisaPDF — V1/V2/V3 e análise documental por projetos

Interface local em Flask para as lógicas preservadas de `varredura_pdf_v1.py`
`varredura_pdf_v2.py`. A interface, os gráficos e o tema são compartilhados;
cada versão mantém seu próprio analisador e sua própria geração de Excel.

## Executar localmente

No PowerShell, dentro desta pasta, use o ambiente virtual já preparado:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app db upgrade
.\.venv\Scripts\python.exe -m flask --app app seed-platform
.\.venv\Scripts\python.exe -m flask --app app create-admin
.\.venv\Scripts\python.exe app.py
```

Abra [http://127.0.0.1:5000/login](http://127.0.0.1:5000/login). O comando
`create-admin` solicita nome, e-mail e senha (mínimo 12 caracteres) sem gravá-la
no projeto. Execute-o somente uma vez para cada conta administrativa.

Defina `FLASK_SECRET_KEY` como segredo aleatório e estável antes de iniciar em
produção. Sem essa variável, cada processo usa um segredo efêmero para
desenvolvimento e as sessões são encerradas ao reiniciar. O banco e os
snapshots usam por padrão `outputs/platform/`; altere esse diretório com
`PESQUISAPDF_DATA_DIR` e a URL do banco com `PESQUISAPDF_DATABASE_URL`.
Defina `PESQUISAPDF_HTTPS=1` somente sob HTTPS para exigir cookie `Secure`.

Para preparar o projeto em outro computador, crie o ambiente virtual com uma
instalação local de Python 3.10 ou superior e instale as dependências antes de
executar o comando acima:

```powershell
<caminho-do-python> -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Uso

O cadastro comum cria uma conta `user` ativa no Plano Estudante, com acesso
gratuito às duas ferramentas inicialmente incluídas nesse plano. O admin pode
alterar plano, forma de acesso, validade e permissões individuais em `/admin`.
Planos e formas de acesso são registros distintos; concessões anteriores são
preservadas para auditoria. Ainda não há cobrança real.

Após entrar, abra `/projetos`, crie um projeto e selecione uma ou mais
bibliotecas. A biblioteca oficial **Relações raciais** contém exatamente o seed
original de 8 grupos, 75 entidades e 117 variantes. Ela não é editável pelo
usuário. O projeto recebe sua própria versão `v1.0`, cujo histórico é
independente dos demais projetos. A análise documental fica em
`/analise-documental`; `/historico-racial` redireciona para a nova entrada.

O vocabulário global anterior em `outputs/historico_racial/vocabulario/` não é
apagado nem migrado automaticamente, pois não possui proprietário associado.
Novos jobs usam somente o snapshot do projeto capturado no início do
processamento. Resultados e progresso continuam temporários em memória; não
constituem ainda um corpus persistente.

Na ferramenta legada em `/`, após autenticação e permissão:

1. Selecione um ou vários PDFs.
2. Informe termos separados por ponto e vírgula, envie um `.txt`, ou use ambos.
3. Escolha a metodologia V1, V2 ou V3 e clique em **Analisar**.
4. Baixe as planilhas da versão escolhida e explore os gráficos na página de resultados.

O TXT aceita um termo por linha ou termos separados por ponto e vírgula. Termos compostos, como `Rio Grande do Norte`, são mantidos como uma única busca. A sintaxe opcional `CATEGORIA|termo` continua compatível com os scripts de referência, mas a categoria não é exportada nas abas Excel.

## Versões e planilhas

- **V1: Busca lexical detalhada:** preserva o analisador e as planilhas da V1.
- **V2: Busca lexical (planilha simplificada):** gera arquivos `resultado_<pdf>_v2.xlsx` e, para vários PDFs,
  `resultado_todos_pdfs_v2.xlsx`. A aba `Ocorrencias` contém exatamente:
  `ID livro`, `Termo`, `Unidade`, `Capítulo`, `Seção`, `Subseção`, `Página`,
  `Tipo de ocorrência`, `Contexto sociológico` e `Descrição`. Os campos
  Unidade, Capítulo, Seção, Subseção e Tipo de ocorrência ficam vazios para
  preenchimento manual.
- **V3: Busca híbrida (lexical + semântica):** combina a busca lexical/morfológica com busca semântica local. Gera
  `<pdf>_v3.xlsx` e, para vários PDFs, `resultado_todos_pdfs_v3.xlsx`, com as
  abas `Resultados`, `Resumo lexical`, `Resumo semântico`, `Resumo por consulta`,
  `Termos pesquisados` e `Diagnostico OCR`. A aba `Resultados` separa
  explicitamente evidências `Lexical`, `Morfológica`, `Semântica` e
  `Lexical + semântica`.

## Busca semântica V3

A V3 usa localmente o modelo
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. O modelo é
carregado somente quando a opção semântica é selecionada e fica em cache na
memória durante a execução. Na primeira execução, o pacote pode baixar o
modelo para o cache local; depois disso, reutiliza a cópia existente.

Resultados semânticos são aproximações de sentido, não comprovação de ocorrência
literal. Revise a coluna **Validação manual** antes de usar a análise em
conclusões de pesquisa.

## OCR

Para PDFs escaneados, instale o [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) e, de preferência, o idioma português. PDFs com texto extraível não dependem do OCR.

## Pastas de trabalho

- `uploads/`: PDFs enviados durante a análise atual.
- `outputs/`: planilhas Excel produzidas.

Os arquivos `varredura_pdf_v1.py` e `varredura_pdf_v2.py` permanecem intactos como referências. Os módulos `analyzer/v1.py`, `analyzer/v2.py` e `analyzer/v3.py` mantêm as versões separadas para a interface.

## Docker

O `Dockerfile` usa Python 3.12 slim, Gunicorn e Tesseract com os idiomas
português e inglês. A V3 reserva `/app/.cache` para o cache gravável do modelo
semântico. Após instalar o Docker, a imagem pode ser construída com:

```powershell
docker build -t varredura-pdfs .
docker run --rm -p 5000:5000 -v "${PWD}/outputs:/app/outputs" -e FLASK_SECRET_KEY="<segredo-aleatorio-estavel>" varredura-pdfs
```

O container aplica a migração e confere os seeds antes do Gunicorn iniciar.
Monte `/app/outputs` como volume persistente: nele ficam banco, snapshots e
planilhas. O padrão atual de um worker e jobs em memória **não** é adequado a
múltiplas instâncias; antes de escalar em AWS, planeje PostgreSQL,
armazenamento compartilhado para resultados e uma fila de jobs persistente.
Não há integração de pagamento, reprocessamento automático nem exportador
padronizado da ferramenta documental nesta etapa.
