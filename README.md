# Varredura de PDFs — aplicação local V1 e V2

Interface local em Flask para as lógicas preservadas de `varredura_pdf_v1.py`
e `varredura_pdf_v2.py`. A interface, os gráficos e o tema são compartilhados;
cada versão mantém seu próprio analisador e sua própria geração de Excel.

## Executar localmente

No PowerShell, dentro desta pasta, use o ambiente virtual já preparado:

```powershell
.\.venv\Scripts\python.exe app.py
```

Abra [http://127.0.0.1:5000](http://127.0.0.1:5000).

Para preparar o projeto em outro computador, crie o ambiente virtual com uma
instalação local de Python 3.10 ou superior e instale as dependências antes de
executar o comando acima:

```powershell
<caminho-do-python> -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Uso

1. Selecione um ou vários PDFs.
2. Informe termos separados por ponto e vírgula, envie um `.txt`, ou use ambos.
3. Escolha V1 ou V2 e clique em **Analisar**.
4. Baixe as planilhas da versão escolhida e explore os gráficos na página de resultados.

O TXT aceita um termo por linha ou termos separados por ponto e vírgula. Termos compostos, como `Rio Grande do Norte`, são mantidos como uma única busca. A sintaxe opcional `CATEGORIA|termo` continua compatível com os scripts de referência, mas a categoria não é exportada nas abas Excel.

## Versões e planilhas

- **V1:** preserva o analisador e as planilhas da V1.
- **V2:** gera arquivos `resultado_<pdf>_v2.xlsx` e, para vários PDFs,
  `resultado_todos_pdfs_v2.xlsx`. A aba `Ocorrencias` contém exatamente:
  `ID livro`, `Termo`, `Unidade`, `Capítulo`, `Seção`, `Subseção`, `Página`,
  `Tipo de ocorrência`, `Contexto sociológico` e `Descrição`. Os campos
  Unidade, Capítulo, Seção, Subseção e Tipo de ocorrência ficam vazios para
  preenchimento manual.

## OCR

Para PDFs escaneados, instale o [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) e, de preferência, o idioma português. PDFs com texto extraível não dependem do OCR.

## Pastas de trabalho

- `uploads/`: PDFs enviados durante a análise atual.
- `outputs/`: planilhas Excel produzidas.

Não há login, banco de dados, histórico de análises ou deploy nesta primeira versão. Os arquivos `varredura_pdf_v1.py` e `varredura_pdf_v2.py` permanecem intactos como referências. Os módulos `analyzer/v1.py` e `analyzer/v2.py` mantêm as versões separadas para a interface.
