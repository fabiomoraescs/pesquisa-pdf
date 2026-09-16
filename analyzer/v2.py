"""Ponte reutilizável para a lógica analítica preservada da V2.

O arquivo ``varredura_pdf_v2.py`` permanece como a referência íntegra da
versão V2. Esta ponte expõe suas funções para a aplicação web sem misturar
a análise ou a exportação V2 com a V1.
"""

from varredura_pdf_v2 import (
    analisar_pdf,
    carregar_termos_referencia,
    configurar_tesseract,
    contar_ocorrencias,
    criar_regex,
    criar_resumos,
    dataframe_vazio,
    descrever_ocorrencia,
    escolher_idioma_ocr,
    formatar_excel,
    gerar_variacoes_palavra,
    gerar_variacoes_termo,
    identificar_contexto_sociologico,
    limpar_texto,
    normalizar,
    salvar_excel_completo,
)

__all__ = [
    "analisar_pdf",
    "carregar_termos_referencia",
    "configurar_tesseract",
    "contar_ocorrencias",
    "criar_regex",
    "criar_resumos",
    "dataframe_vazio",
    "descrever_ocorrencia",
    "escolher_idioma_ocr",
    "formatar_excel",
    "gerar_variacoes_palavra",
    "gerar_variacoes_termo",
    "identificar_contexto_sociologico",
    "limpar_texto",
    "normalizar",
    "salvar_excel_completo",
]
