"""Metadados de reprodutibilidade compartilhados pelos arquivos Excel.

Este módulo não participa da extração, localização ou classificação dos
resultados. Ele apenas acrescenta uma aba de auditoria depois que cada
analisador concluiu a sua exportação normal.
"""

from __future__ import annotations

from collections import defaultdict
from copy import copy
from datetime import datetime
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


ABA_PARAMETROS = "Parâmetros da análise"

DEFINICAO_OPERACIONAL = (
    "Os resultados produzidos pela ferramenta devem ser tratados como candidatos "
    "à análise. A presença de um termo ou a proximidade semântica com uma consulta "
    "não constitui, por si só, evidência de pertinência substantiva ao tema. A "
    "validação final depende da interpretação do pesquisador no contexto do trecho."
)

OBSERVACAO_SEMANTICA = (
    "Correspondências semânticas não representam ocorrências textuais do termo "
    "pesquisado. O valor de similaridade indica proximidade entre representações "
    "vetoriais dos textos e não corresponde a probabilidade, certeza ou validação "
    "científica."
)


def aplicar_apresentacao_sem_quebra(workbook) -> None:
    """Remove apenas a quebra visual, preservando conteúdo e demais estilos."""
    for aba in workbook.worksheets:
        for linha in aba.iter_rows():
            for celula in linha:
                alinhamento = copy(celula.alignment)
                alinhamento.wrap_text = False
                celula.alignment = alinhamento

        # Mantém o cabeçalho como definido por cada exportador. Linhas de dados
        # sem altura explícita recebem a altura compacta padrão do Excel.
        for indice_linha in range(2, aba.max_row + 1):
            dimensao = aba.row_dimensions[indice_linha]
            if dimensao.height is None:
                dimensao.height = 15


def gerar_ids_resultado(
    resultados: pd.DataFrame,
    versao: str,
    indice_livro_padrao: int = 1,
) -> list[str]:
    """Cria IDs determinísticos por método, PDF e sequência de resultado.

    ``_indice_livro`` é acrescentado somente pela orquestração da aplicação
    quando há múltiplos PDFs. Chamadas diretas aos exportadores continuam
    recebendo o índice ``001`` sem depender do índice do DataFrame.
    """
    try:
        indice_livro_padrao = max(1, int(indice_livro_padrao))
    except (TypeError, ValueError):
        indice_livro_padrao = 1

    if resultados.empty:
        return []

    if "_indice_livro" in resultados.columns:
        indices_livro = resultados["_indice_livro"].tolist()
    else:
        indices_livro = [indice_livro_padrao] * len(resultados)

    sequencias: dict[int, int] = defaultdict(int)
    identificadores: list[str] = []
    for indice_bruto in indices_livro:
        try:
            indice_livro = int(indice_bruto)
        except (TypeError, ValueError):
            indice_livro = indice_livro_padrao
        indice_livro = max(1, indice_livro)
        sequencias[indice_livro] += 1
        identificadores.append(
            f"{versao.upper()}-{indice_livro:03d}-{sequencias[indice_livro]:06d}"
        )
    return identificadores


def _valores_unicos(df: pd.DataFrame, coluna: str) -> list[str]:
    if coluna not in df.columns:
        return []
    valores: list[str] = []
    for valor in df[coluna].dropna().tolist():
        texto = str(valor).strip()
        if texto and texto not in valores:
            valores.append(texto)
    return valores


def _sim_ou_nao(valor: Any) -> str:
    return "Sim" if bool(valor) else "Não"


def _ocr_utilizado(diagnosticos: pd.DataFrame) -> str:
    if "paginas_processadas_com_OCR" not in diagnosticos.columns:
        return "Não informado"
    paginas = pd.to_numeric(
        diagnosticos["paginas_processadas_com_OCR"], errors="coerce"
    ).fillna(0)
    quantidade = int(paginas.sum())
    return f"Utilizado em {quantidade} página(s)" if quantidade else "Não utilizado"


def _idiomas_ocr(diagnosticos: pd.DataFrame) -> str:
    idiomas = [
        idioma
        for idioma in _valores_unicos(diagnosticos, "idioma_OCR")
        if idioma.casefold() not in {"não utilizado", "nao utilizado"}
    ]
    return "; ".join(idiomas) if idiomas else "Não utilizado"


def _configuracao_v3(
    diagnosticos: pd.DataFrame, configuracoes: dict[str, Any] | None
) -> tuple[bool, bool, str]:
    configuracoes = configuracoes or {}
    lexical = configuracoes.get("incluir_lexical")
    semantica = configuracoes.get("incluir_semantica")
    limiar = configuracoes.get("limiar_semantico")

    if lexical is None and "busca_lexical_ativada" in diagnosticos.columns:
        lexical = bool(diagnosticos["busca_lexical_ativada"].fillna(False).any())
    if semantica is None and "busca_semantica_ativada" in diagnosticos.columns:
        semantica = bool(diagnosticos["busca_semantica_ativada"].fillna(False).any())
    if limiar is None and "limiar_semantico" in diagnosticos.columns:
        valores = pd.to_numeric(diagnosticos["limiar_semantico"], errors="coerce").dropna()
        limiar = float(valores.iloc[0]) if not valores.empty else None

    lexical = bool(lexical)
    semantica = bool(semantica)
    if semantica and limiar is not None:
        try:
            limiar_texto = f"{float(limiar):.2f}"
        except (TypeError, ValueError):
            limiar_texto = "Não informado"
    else:
        limiar_texto = "Não aplicável"
    return lexical, semantica, limiar_texto


def _parametros(
    versao: str,
    termos: pd.DataFrame,
    diagnosticos: pd.DataFrame,
    configuracoes: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    arquivos = _valores_unicos(diagnosticos, "arquivo")
    consultas = _valores_unicos(termos, "Termo")
    abrangencia = (
        "Análise consolidada de múltiplos PDFs"
        if len(arquivos) > 1
        else "Análise de um PDF"
    )
    comum = [
        ("Método de varredura", ""),
        ("Abrangência da análise", abrangencia),
        ("Arquivo(s) analisado(s)", "\n".join(arquivos) or "Não informado"),
        ("Consultas utilizadas", "\n".join(consultas) or "Não informado"),
        ("Data e hora da análise", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")),
        ("Unidade de recuperação/análise", ""),
        ("Busca lexical/morfológica", ""),
        ("Busca semântica", ""),
        ("Limiar semântico", ""),
        ("Modelo de embeddings", ""),
        ("OCR", _ocr_utilizado(diagnosticos)),
        ("Idiomas OCR", _idiomas_ocr(diagnosticos)),
        ("Definição operacional", DEFINICAO_OPERACIONAL),
        ("Observações metodológicas", ""),
    ]

    if versao == "v1":
        valores = {
            "Método de varredura": "V1: Busca lexical detalhada",
            "Unidade de recuperação/análise": "bloco/parágrafo textual identificado na página",
            "Busca lexical/morfológica": "Sim",
            "Busca semântica": "Não",
            "Limiar semântico": "Não aplicável",
            "Modelo de embeddings": "Não aplicável",
            "Observações metodológicas": "A revisão qualitativa do pesquisador permanece necessária.",
        }
    elif versao == "v2":
        valores = {
            "Método de varredura": "V2: Busca lexical (planilha simplificada)",
            "Unidade de recuperação/análise": "bloco/parágrafo textual identificado na página",
            "Busca lexical/morfológica": "Sim",
            "Busca semântica": "Não",
            "Limiar semântico": "Não aplicável",
            "Modelo de embeddings": "Não aplicável",
            "Observações metodológicas": "A revisão qualitativa do pesquisador permanece necessária.",
        }
    elif versao == "v3":
        lexical, semantica, limiar = _configuracao_v3(
            diagnosticos, configuracoes
        )
        valores = {
            "Método de varredura": "V3: Busca híbrida (lexical + semântica)",
            "Unidade de recuperação/análise": "bloco/parágrafo lexical ou trecho recuperado pela busca semântica",
            "Busca lexical/morfológica": _sim_ou_nao(lexical),
            "Busca semântica": _sim_ou_nao(semantica),
            "Limiar semântico": limiar,
            "Modelo de embeddings": (
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
                if semantica
                else "Não utilizado (busca semântica desativada)"
            ),
            "Observações metodológicas": OBSERVACAO_SEMANTICA,
        }
    else:
        raise ValueError(f"Versão não suportada para auditoria: {versao}")

    return [(chave, valores.get(chave, valor)) for chave, valor in comum]


def adicionar_aba_parametros(
    arquivo_saida: Path | str,
    versao: str,
    termos: pd.DataFrame,
    diagnosticos: pd.DataFrame,
    configuracoes: dict[str, Any] | None = None,
) -> None:
    """Acrescenta uma aba formatada de parâmetros sem alterar abas existentes."""
    workbook = load_workbook(arquivo_saida)
    if ABA_PARAMETROS in workbook.sheetnames:
        del workbook[ABA_PARAMETROS]

    aba = workbook.create_sheet(ABA_PARAMETROS)
    aba.append(["Parâmetro", "Valor"])
    for parametro, valor in _parametros(versao, termos, diagnosticos, configuracoes):
        aba.append([parametro, valor])

    aba.freeze_panes = "A2"
    aba.auto_filter.ref = aba.dimensions
    aba.sheet_view.showGridLines = False
    for celula in aba[1]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="1F4E78")
        celula.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)
    for linha in aba.iter_rows(min_row=2):
        for celula in linha:
            celula.alignment = Alignment(vertical="top", wrap_text=False)

    aba.column_dimensions["A"].width = 34
    aba.column_dimensions["B"].width = 105
    aplicar_apresentacao_sem_quebra(workbook)
    workbook.save(arquivo_saida)
