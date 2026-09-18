"""Orquestração compartilhada para a interface das versões V1, V2 e V3.

Este módulo só prepara entradas, coordena arquivos e agrega dados para o
dashboard. A extração, análise e exportação Excel continuam delegadas aos
módulos específicos de cada versão.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pymupdf

from . import v1, v2, v3


ANALISADORES = {"v1": v1, "v2": v2, "v3": v3}


def obter_analisador(versao: str):
    """Retorna o módulo analítico solicitado sem combinar as versões."""
    try:
        return ANALISADORES[versao]
    except KeyError as erro:
        raise ValueError("Versão de análise inválida.") from erro


def ler_arquivo_termos(conteudo: bytes) -> str:
    """Decodifica um TXT enviado, aceitando os encodings mais comuns."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return conteudo.decode(encoding)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("utf-8", errors="replace")


def _partes_de_termos(texto: str) -> list[str]:
    """Aceita termos separados por ponto e vírgula ou por uma linha cada."""
    return [parte.strip() for parte in re.split(r"[;\r\n]+", texto or "") if parte.strip()]


def montar_termos(
    texto_formulario: str, texto_arquivo: str, versao: str = "v1"
) -> list[dict[str, str]]:
    """Combina as duas entradas mantendo o tratamento da versão escolhida.

    A barra vertical opcional preserva o recurso do script original para
    informar uma categoria manual (por exemplo, ``REGIÃO|Nordeste``). Termos
    compostos não são divididos: apenas ponto e vírgula e quebras de linha
    separam entradas.
    """
    analisador = obter_analisador(versao)
    mapa_categorias: dict[str, str] = {}
    for item in analisador.carregar_termos_referencia():
        chave = analisador.normalizar(item["termo"]).strip()
        if chave not in mapa_categorias:
            mapa_categorias[chave] = item["categoria"]

    termos: list[dict[str, str]] = []
    vistos: set[str] = set()

    # A ordem é deliberada: o que foi escrito no formulário tem precedência
    # de exibição quando o mesmo termo também estiver no TXT.
    for parte in _partes_de_termos(texto_formulario) + _partes_de_termos(texto_arquivo):
        categoria_manual = None
        termo = parte

        if "|" in parte:
            possivel_categoria, possivel_termo = parte.split("|", 1)
            if possivel_categoria.strip() and possivel_termo.strip():
                categoria_manual = possivel_categoria.strip()
                termo = possivel_termo.strip()

        termo = termo.strip()
        if not termo:
            continue

        chave = analisador.normalizar(termo).strip()
        if chave in vistos:
            continue

        vistos.add(chave)
        termos.append(
            {
                "categoria": categoria_manual
                or mapa_categorias.get(chave)
                or "TERMO INFORMADO",
                "termo": termo,
            }
        )

    return termos


def _nome_individual(pdf: Path, versao: str) -> str:
    """Preserva o nome do PDF, aplicando só a sanitização necessária."""
    nome_seguro = re.sub(r'[<>:"/\\|?*]', "_", pdf.stem)
    if versao == "v3":
        return f"{nome_seguro}_v3.xlsx"
    return f"resultado_{nome_seguro}_{versao}.xlsx"


def _contar_paginas_para_progresso(pdfs: list[Path]) -> list[int | None]:
    """Lê apenas metadados baratos para ponderar o progresso por PDF."""
    contagens: list[int | None] = []
    for pdf in pdfs:
        documento = None
        try:
            documento = pymupdf.open(pdf)
            contagens.append(len(documento))
        except Exception:
            # Uma consulta de metadados não pode impedir o fluxo analítico.
            contagens.append(None)
        finally:
            if documento is not None:
                documento.close()
    return contagens


def executar_analises(
    pdfs: list[Path],
    termos: list[dict[str, str]],
    pasta_saida: Path,
    versao: str = "v1",
    configuracoes: dict | None = None,
    progress_callback=None,
) -> dict:
    """Executa a sequência da versão solicitada, sem interação de terminal.

    ``progress_callback`` é estritamente observacional: recebe apenas
    metadados de etapas já executadas. Com ``None`` o fluxo analítico e as
    chamadas originais permanecem os mesmos.
    """
    analisador = obter_analisador(versao)
    pasta_saida.mkdir(parents=True, exist_ok=True)
    df_termos = pd.DataFrame(termos).rename(
        columns={"categoria": "Categoria do termo", "termo": "Termo"}
    )

    todos_registros: list[dict] = []
    todos_diagnosticos: list[dict] = []
    arquivos: list[dict[str, str]] = []
    erros: list[dict[str, str]] = []
    contagens_paginas = (
        _contar_paginas_para_progresso(pdfs)
        if progress_callback is not None
        else []
    )
    total_paginas_global = (
        sum(contagem for contagem in contagens_paginas if contagem is not None)
        if contagens_paginas and all(contagem is not None for contagem in contagens_paginas)
        else None
    )

    def emitir(evento: dict) -> None:
        if progress_callback is not None:
            progress_callback(evento)

    for indice_livro, pdf in enumerate(pdfs, start=1):
        def progresso_do_livro(evento: dict) -> None:
            metadados_paginas: dict[str, int] = {}
            if total_paginas_global:
                paginas_anteriores = sum(
                    contagem or 0 for contagem in contagens_paginas[: indice_livro - 1]
                )
                metadados_paginas = {
                    "paginas_total_global": total_paginas_global,
                    "paginas_anteriores": paginas_anteriores,
                    "paginas_arquivo": contagens_paginas[indice_livro - 1] or 0,
                }
                pagina_atual = evento.get("pagina_atual")
                if isinstance(pagina_atual, int):
                    metadados_paginas["paginas_processadas_total"] = min(
                        total_paginas_global,
                        paginas_anteriores + max(0, pagina_atual),
                    )
            emitir(
                {
                    **evento,
                    "arquivo": pdf.name,
                    "arquivo_indice": indice_livro,
                    "arquivos_total": len(pdfs),
                    **metadados_paginas,
                }
            )

        if progress_callback is not None:
            progresso_do_livro(
                {
                    "fase": "preparando_pdf",
                    "etapa": "Preparando análise…",
                }
            )
        try:
            if progress_callback is None and versao == "v3":
                ocorrencias, diagnostico = analisador.analisar_pdf(
                    pdf, termos, configuracoes
                )
            elif progress_callback is None:
                ocorrencias, diagnostico = analisador.analisar_pdf(pdf, termos)
            elif versao == "v3":
                ocorrencias, diagnostico = analisador.analisar_pdf(
                    pdf,
                    termos,
                    configuracoes,
                    progress_callback=progresso_do_livro,
                )
            else:
                ocorrencias, diagnostico = analisador.analisar_pdf(
                    pdf,
                    termos,
                    progress_callback=progresso_do_livro,
                )
        except Exception as erro:  # permite que os demais PDFs sejam processados
            erros.append({"arquivo": pdf.name, "mensagem": str(erro)})
            if progress_callback is not None:
                progresso_do_livro(
                    {
                        "fase": "pdf_com_erro",
                        "etapa": "Preparando resultados…",
                    }
                )
            continue

        # Metadado interno de exportação: preserva a posição do PDF na análise
        # para que os IDs individuais e consolidados sejam idênticos e únicos.
        for ocorrencia in ocorrencias:
            ocorrencia["_indice_livro"] = indice_livro

        df_livro = (
            pd.DataFrame(ocorrencias) if ocorrencias else analisador.dataframe_vazio()
        )
        arquivo_saida = pasta_saida / _nome_individual(pdf, versao)
        if progress_callback is not None:
            progresso_do_livro(
                {
                    "fase": "gerando_planilha",
                    "etapa": "Gerando planilha…",
                }
            )
        analisador.salvar_excel_completo(
            arquivo_saida,
            df_livro,
            df_termos,
            pd.DataFrame([diagnostico]),
            configuracoes=configuracoes,
            indice_livro=indice_livro,
        )

        arquivos.append(
            {"nome": arquivo_saida.name, "rotulo": f"Baixar Excel — {pdf.name}"}
        )
        todos_registros.extend(ocorrencias)
        todos_diagnosticos.append(diagnostico)
        if progress_callback is not None:
            progresso_do_livro(
                {
                    "fase": "pdf_concluido",
                    "etapa": "Preparando resultados…",
                }
            )

    # Cada versão gera o consolidado quando o usuário seleciona mais de um PDF.
    if len(pdfs) > 1:
        if progress_callback is not None:
            emitir(
                {
                    "fase": "gerando_consolidado",
                    "etapa": "Gerando planilha consolidada…",
                    "arquivos_total": len(pdfs),
                }
            )
        df_todos = (
            pd.DataFrame(todos_registros)
            if todos_registros
            else analisador.dataframe_vazio()
        )
        arquivo_consolidado = pasta_saida / f"resultado_todos_pdfs_{versao}.xlsx"
        analisador.salvar_excel_completo(
            arquivo_consolidado,
            df_todos,
            df_termos,
            pd.DataFrame(todos_diagnosticos),
            configuracoes=configuracoes,
            indice_livro=1,
        )
        arquivos.append(
            {
                "nome": arquivo_consolidado.name,
                "rotulo": "Baixar Excel consolidado",
                "consolidado": True,
            }
        )

    if progress_callback is not None:
        emitir(
            {
                "fase": "preparando_resultados",
                "etapa": "Preparando resultados…",
                "arquivos_total": len(pdfs),
            }
        )

    df_ocorrencias = (
        pd.DataFrame(todos_registros)
        if todos_registros
        else analisador.dataframe_vazio()
    )
    df_diagnosticos = pd.DataFrame(todos_diagnosticos)

    return {
        "arquivos": arquivos,
        "erros": erros,
        "quantidade_pdfs": len(todos_diagnosticos),
        "quantidade_termos": len(termos),
        "ocorrencias": df_ocorrencias,
        "diagnosticos": df_diagnosticos,
        "termos": termos,
        "versao": versao,
    }


def _serie_por_coluna(
    dataframe: pd.DataFrame, coluna: str, ordem: list[str] | None = None
) -> dict[str, list]:
    if dataframe.empty:
        valores: dict[str, int] = {}
    else:
        agrupado = dataframe.groupby(coluna, dropna=False)["_quantidade_no_registro"].sum()
        valores = {str(indice or "Não informado"): int(valor) for indice, valor in agrupado.items()}

    rotulos = ordem or sorted(valores, key=str.casefold)
    return {"rotulos": rotulos, "valores": [valores.get(rotulo, 0) for rotulo in rotulos]}


def _rotulo_dashboard(valor: object) -> str:
    """Normaliza somente rótulos de apresentação, sem tocar nos dados da V1."""
    if pd.isna(valor):
        return "Não informado"
    texto = str(valor).strip()
    return texto or "Não informado"


def _base_dashboard_ponderada(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Cria uma cópia ponderada apenas para agregações visuais."""
    if dataframe.empty:
        return dataframe.copy()

    base = dataframe.copy()
    base["_peso_dashboard"] = pd.to_numeric(
        base["_quantidade_no_registro"], errors="coerce"
    ).fillna(0)
    base["_livro_dashboard"] = base["ID livro"].map(_rotulo_dashboard)
    base["_termo_dashboard"] = base["Termo"].map(_rotulo_dashboard)
    base["_contexto_dashboard"] = base[
        "Contexto sociológico da ocorrência"
    ].map(_rotulo_dashboard)
    return base


def _ordenar_por_total(serie: pd.Series) -> list[str]:
    return sorted(
        serie.index.tolist(),
        key=lambda item: (-float(serie.loc[item]), str(item).casefold()),
    )


def _dados_contextos_por_livro(base: pd.DataFrame, livros: list[str]) -> dict:
    """Matriz bruta e percentual para barras 100% empilhadas."""
    if base.empty:
        return {"livros": livros, "contextos": [], "contagens": [], "percentuais": []}

    contextos = sorted(base["_contexto_dashboard"].unique().tolist(), key=str.casefold)
    tabela = base.pivot_table(
        index="_contexto_dashboard",
        columns="_livro_dashboard",
        values="_peso_dashboard",
        aggfunc="sum",
        fill_value=0,
    ).reindex(index=contextos, columns=livros, fill_value=0)
    contagens = [[int(valor) for valor in linha] for linha in tabela.to_numpy().tolist()]
    totais = [sum(contagens[indice][coluna] for indice in range(len(contextos))) for coluna in range(len(livros))]
    percentuais = [
        [
            (valor * 100 / totais[coluna]) if totais[coluna] else 0
            for coluna, valor in enumerate(linha)
        ]
        for linha in contagens
    ]
    return {
        "livros": livros,
        "contextos": contextos,
        "contagens": contagens,
        "percentuais": percentuais,
    }


def _dados_pareto(base: pd.DataFrame, termos: list[str]) -> dict:
    valores = {termo: 0 for termo in termos}
    if not base.empty:
        agrupado = base.groupby("_termo_dashboard")["_peso_dashboard"].sum()
        valores.update({str(termo): int(valor) for termo, valor in agrupado.items()})

    ordenados = sorted(valores, key=lambda termo: (-valores[termo], termo.casefold()))
    total = sum(valores.values())
    acumulado = 0.0
    linhas = []
    for termo in ordenados:
        quantidade = valores[termo]
        percentual = (quantidade * 100 / total) if total else 0.0
        acumulado += percentual
        linhas.append(
            {
                "termo": termo,
                "ocorrencias": quantidade,
                "percentual": percentual,
                "acumulado": acumulado,
            }
        )
    return {"linhas": linhas}


def _dados_sankey(base: pd.DataFrame, livros: list[str]) -> dict:
    """Fluxo visual limitado sem modificar os registros originais da análise."""
    vazio = {"labels": [], "sources": [], "targets": [], "values": [], "aviso": ""}
    if base.empty:
        return vazio

    limite_termos = 12
    limite_contextos = 10
    totais_termo = base.groupby("_termo_dashboard")["_peso_dashboard"].sum()
    totais_contexto = base.groupby("_contexto_dashboard")["_peso_dashboard"].sum()
    termos_ordenados = _ordenar_por_total(totais_termo)
    contextos_ordenados = _ordenar_por_total(totais_contexto)
    termos_visiveis = set(termos_ordenados[:limite_termos])
    contextos_visiveis = set(contextos_ordenados[:limite_contextos])
    agrupamentos = []
    if len(termos_ordenados) > limite_termos:
        agrupamentos.append(f"termos fora dos {limite_termos} mais frequentes em “Outros termos”")
    if len(contextos_ordenados) > limite_contextos:
        agrupamentos.append(f"contextos fora dos {limite_contextos} mais frequentes em “Outros contextos”")

    visual = base.copy()
    visual["_termo_visual"] = visual["_termo_dashboard"].where(
        visual["_termo_dashboard"].isin(termos_visiveis), "Outros termos"
    )
    visual["_contexto_visual"] = visual["_contexto_dashboard"].where(
        visual["_contexto_dashboard"].isin(contextos_visiveis), "Outros contextos"
    )
    termos = _ordenar_por_total(visual.groupby("_termo_visual")["_peso_dashboard"].sum())
    contextos = _ordenar_por_total(visual.groupby("_contexto_visual")["_peso_dashboard"].sum())
    livros_com_dados = [livro for livro in livros if livro in set(visual["_livro_dashboard"])]
    livros_com_dados.extend(
        livro
        for livro in sorted(visual["_livro_dashboard"].unique().tolist(), key=str.casefold)
        if livro not in livros_com_dados
    )

    labels = [
        *[f"Termo: {termo}" for termo in termos],
        *[f"Contexto: {contexto}" for contexto in contextos],
        *[f"Livro: {livro}" for livro in livros_com_dados],
    ]
    indice_termo = {termo: indice for indice, termo in enumerate(termos)}
    deslocamento_contextos = len(termos)
    indice_contexto = {
        contexto: deslocamento_contextos + indice
        for indice, contexto in enumerate(contextos)
    }
    deslocamento_livros = deslocamento_contextos + len(contextos)
    indice_livro = {
        livro: deslocamento_livros + indice
        for indice, livro in enumerate(livros_com_dados)
    }

    sources: list[int] = []
    targets: list[int] = []
    values: list[int] = []
    termo_contexto = visual.groupby(["_termo_visual", "_contexto_visual"])["_peso_dashboard"].sum()
    for (termo, contexto), quantidade in termo_contexto.items():
        sources.append(indice_termo[termo])
        targets.append(indice_contexto[contexto])
        values.append(int(quantidade))
    contexto_livro = visual.groupby(["_contexto_visual", "_livro_dashboard"])["_peso_dashboard"].sum()
    for (contexto, livro), quantidade in contexto_livro.items():
        sources.append(indice_contexto[contexto])
        targets.append(indice_livro[livro])
        values.append(int(quantidade))

    aviso = ""
    if agrupamentos:
        aviso = "Visualização agrupou " + " e ".join(agrupamentos) + "."
    return {"labels": labels, "sources": sources, "targets": targets, "values": values, "aviso": aviso}


def criar_dashboard(resultado: dict) -> dict:
    """Converte dados internos no payload próprio de cada dashboard."""
    if resultado.get("versao") == "v3":
        return v3.criar_dashboard(resultado)

    df = resultado["ocorrencias"].copy()
    termos = [item["termo"] for item in resultado["termos"]]
    livros = [Path(item["arquivo"]).stem for item in resultado["diagnosticos"].to_dict("records")]

    por_termo = _serie_por_coluna(df, "Termo", termos)
    por_livro = _serie_por_coluna(df, "ID livro", livros)
    por_contexto = _serie_por_coluna(df, "Contexto sociológico da ocorrência")
    base = _base_dashboard_ponderada(df)

    matriz = [[0 for _ in livros] for _ in termos]
    if not df.empty and termos and livros:
        pivot = df.pivot_table(
            index="Termo",
            columns="ID livro",
            values="_quantidade_no_registro",
            aggfunc="sum",
            fill_value=0,
        )
        for indice_termo, termo in enumerate(termos):
            for indice_livro, livro in enumerate(livros):
                if termo in pivot.index and livro in pivot.columns:
                    matriz[indice_termo][indice_livro] = int(pivot.loc[termo, livro])

    total_ocorrencias = (
        int(df["_quantidade_no_registro"].sum()) if not df.empty else 0
    )
    total_ocr = (
        int(resultado["diagnosticos"]["paginas_processadas_com_OCR"].sum())
        if not resultado["diagnosticos"].empty
        else 0
    )

    return {
        "indicadores": {
            "pdfs": resultado["quantidade_pdfs"],
            "termos": resultado["quantidade_termos"],
            "ocorrencias": total_ocorrencias,
            "ocr": total_ocr,
        },
        "por_termo": por_termo,
        "por_livro": por_livro,
        "por_contexto": por_contexto,
        "comparacao": {"termos": termos, "livros": livros, "matriz": matriz},
        "contextos_por_livro": _dados_contextos_por_livro(base, livros),
        "pareto": _dados_pareto(base, termos),
        "sankey": _dados_sankey(base, livros),
    }
