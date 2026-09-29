"""Contextos funcionais e a composição do painel do Assistente Análysis."""

from __future__ import annotations

from typing import Any

from .assistant_project_context import (
    TOOL_TO_CONTEXT,
    context_has_observed_records,
    resolve_project_context,
)


ASSISTANT_CONTEXTS: dict[str, dict[str, Any]] = {
    "home": {
        "intro": (
            "Você está no Dashboard. Posso ajudar a conhecer o Análysis, escolher "
            "uma ferramenta e entender como começar uma análise."
        ),
        "suggestions": [
            {"question": "O que é o Análysis?", "answer": (
                "O Análysis reúne ferramentas para localizar, organizar e examinar "
                "evidências em PDFs. Ele apoia a pesquisa; a interpretação dos resultados "
                "continua sendo sua."
            )},
            {"question": "Qual ferramenta devo usar?", "answer": (
                "Use Busca por termos quando já tiver palavras ou expressões a pesquisar; "
                "Busca estruturada quando trabalhar com bibliotecas de termos; e Análise "
                "quali-dados para ler, codificar e registrar memos em trechos dos PDFs. "
                "A disponibilidade depende do seu acesso."
            )},
            {"question": "Qual a diferença entre as três modalidades?", "answer": (
                "Busca por termos parte de consultas que você informa. Busca estruturada "
                "usa bibliotecas de termos organizadas. Análise quali-dados oferece leitura "
                "do corpus, códigos, memos e relatório de codificação."
            )},
            {"question": "Como começo um projeto?", "answer": (
                "Abra a modalidade desejada no menu, escolha Novo projeto e informe um nome. "
                "Depois, adicione PDFs ou uma base conforme o fluxo daquela ferramenta."
            )},
            {"question": "Que análises posso fazer aqui?", "answer": (
                "Você pode pesquisar ocorrências textuais, trabalhar com bibliotecas de "
                "termos ou organizar trechos com códigos e memos. As bases e relatórios "
                "ajudam a revisar os achados e documentar o percurso da pesquisa."
            )},
        ],
    },
    "projects": {
        "intro": (
            "Você está na área de projetos. Posso ajudar a organizar documentos, "
            "bases de análise e etapas da pesquisa."
        ),
        "suggestions": [
            {"question": "O que é um projeto no Análysis?", "answer": (
                "Um projeto agrupa o trabalho de uma modalidade. Conforme a ferramenta, "
                "ele reúne bases de análise ou documentos e registros qualitativos."
            )},
            {"question": "Como adiciono uma base ou documento?", "answer": (
                "Abra o projeto. Em Busca por termos e Busca estruturada, use Adicionar "
                "base para iniciar uma execução com PDFs. Na Análise quali-dados, use "
                "Adicionar documentos no ambiente do projeto."
            )},
            {"question": "Qual ferramenta devo usar neste projeto?", "answer": (
                "A modalidade é definida ao criar o projeto: Busca por termos, Busca "
                "estruturada ou Análise quali-dados. Para outro tipo de trabalho, crie "
                "um projeto na modalidade correspondente."
            )},
            {"question": "Posso ter mais de uma análise no projeto?", "answer": (
                "Nas ferramentas de busca, um projeto pode reunir diferentes bases de "
                "análise. Na Quali-dados, o projeto mantém seu ambiente de documentos "
                "e registros para continuar a leitura."
            )},
            {"question": "Como organizo meu fluxo de pesquisa?", "answer": (
                "Dê nomes claros aos projetos, registre o objetivo de cada base ou etapa "
                "e revise os resultados no PDF de origem. Preserve também as decisões "
                "metodológicas fora da contagem automática de ocorrências."
            )},
        ],
    },
    "term_search": {
        "intro": (
            "Você está na Busca por termos. Posso orientar a preparação dos termos, "
            "a leitura dos resultados e a descrição do procedimento."
        ),
        "suggestions": [
            {"question": "Para que serve a Busca por termos?", "answer": (
                "Ela localiza palavras e expressões em um ou mais PDFs e guarda cada "
                "execução como Base de análise, com trechos, documentos e páginas de origem."
            )},
            {"question": "Como informo os termos da busca?", "answer": (
                "Separe termos por ponto e vírgula ou coloque um por linha. Expressões "
                "compostas permanecem inteiras. Você também pode enviar um TXT com termos; "
                "não use vírgula ou ponto como separador."
            )},
            {"question": "Como funcionam as variações lexicais?", "answer": (
                "O método Lexical procura a forma informada, flexões morfológicas e "
                "derivações da mesma família lexical. O método Híbrido acrescenta "
                "recuperação por proximidade semântica, que exige revisão humana."
            )},
            {"question": "Como interpreto os resultados?", "answer": (
                "Confira o trecho no contexto, o PDF e a página. Uma ocorrência textual "
                "não prova relevância teórica; similaridade semântica também não significa "
                "identidade de sentido."
            )},
            {"question": "Como descrevo essa busca na metodologia?", "answer": (
                "Registre corpus, termos, método escolhido e critérios de revisão dos "
                "trechos. Diferencie resultados lexicais dos semânticos e explique "
                "como interpretou as evidências."
            )},
        ],
    },
    "structured_search": {
        "intro": (
            "Você está na Busca estruturada. Posso ajudar a entender bibliotecas "
            "de termos, métodos de busca e resultados."
        ),
        "suggestions": [
            {"question": "Para que serve a Busca estruturada?", "answer": (
                "Ela pesquisa PDFs com uma ou mais bibliotecas de termos associadas ao "
                "projeto, produzindo bases de análise que podem ser reabertas e revisadas."
            )},
            {"question": "O que é uma biblioteca de termos?", "answer": (
                "É uma organização de termos, variantes e categorias que orienta a busca "
                "estruturada. A biblioteca ajuda a manter explícito o vocabulário usado."
            )},
            {"question": "Como crio ou importo uma biblioteca?", "answer": (
                "No fluxo do projeto, use Inserir biblioteca para criar manualmente ou "
                "importar uma planilha. A área Gerenciar → Bibliotecas oferece o fluxo "
                "administrativo, conforme suas permissões."
            )},
            {"question": "Como funcionam Lexical e Morfológico?", "answer": (
                "A busca Lexical inclui forma literal, flexões morfológicas e derivações "
                "da mesma família. Morfológico descreve as flexões; nesta tela, não é "
                "uma opção independente. O método Híbrido acrescenta similaridade semântica."
            )},
            {"question": "Como descrevo essa etapa na metodologia?", "answer": (
                "Documente a biblioteca utilizada, suas categorias e variantes, o corpus, "
                "o método escolhido e como revisou as passagens recuperadas."
            )},
        ],
    },
    "qualitative": {
        "intro": (
            "Você está na Análise quali-dados. Posso ajudar a entender os recursos "
            "desta etapa, organizar a leitura e pensar em sua descrição metodológica."
        ),
        "suggestions": [
            {"question": "Qual a diferença entre Literal, Lexical e Semântica?", "answer": (
                "Literal localiza correspondências textuais; Lexical soma flexões e "
                "derivações da mesma família; Semântica também considera trechos de "
                "significado relacionado. Na autocodificação, o código representa a "
                "consulta, enquanto a marcação mostra o trecho concreto encontrado. "
                "Revise os candidatos no contexto."
            )},
            {"question": "Como funciona a Rejeição contextual?", "answer": (
                "Se ativada antes da autocodificação, uma codificação automática que "
                "você excluir pelo × não será recriada pela mesma busca, com o mesmo "
                "código e contexto, neste projeto. Desativada, a exclusão só remove "
                "a codificação."
            )},
            {"question": "Como organizo, renomeio e coloro códigos?", "answer": (
                "Use Códigos no Explorador para organizar seus códigos. Clique em uma "
                "etiqueta na Margem analítica para localizar e destacar o trecho no PDF. "
                "Para renomear o código ou alterar sua cor, clique com o botão direito "
                "na etiqueta. O nome e a cor pertencem ao código e se refletem nas suas etiquetas."
            )},
            {"question": "Como uso a Margem analítica e o Explorador?", "answer": (
                "A Margem mostra os códigos associados aos trechos da página em leitura. "
                "O Explorador reúne documentos, códigos e memos, permitindo navegar "
                "e acompanhar os registros do projeto."
            )},
            {"question": "Como descrevo a análise na metodologia?", "answer": (
                "Registre o corpus, os critérios de seleção e leitura, como criou e "
                "revisou códigos e memos, e quando usou busca ou autocodificação. "
                "Esta orientação é geral: ainda não examino as escolhas do seu projeto."
            )},
        ],
    },
    "coding_report": {
        "intro": (
            "Você está no Relatório de codificação. Posso ajudar a interpretar "
            "os códigos, os trechos e sua exportação."
        ),
        "suggestions": [
            {"question": "O que mostra o Relatório de codificação?", "answer": (
                "Ele reúne os trechos codificados por código, com documento e página. "
                "O link do documento leva ao trecho correspondente no leitor."
            )},
            {"question": "Como interpreto as frequências dos códigos?", "answer": (
                "O número ao lado de cada código conta seus trechos associados. Um "
                "mesmo trecho pode aparecer em mais de um código. Frequência não é, "
                "por si só, importância analítica."
            )},
            {"question": "O que representam as cores dos códigos?", "answer": (
                "A cor identifica visualmente cada código e acompanha suas etiquetas, "
                "o relatório e a célula do código no Excel. Ela ajuda a leitura, "
                "mas não altera contagens nem o conteúdo dos trechos."
            )},
            {"question": "Como uso os trechos codificados?", "answer": (
                "Leia cada passagem no PDF de origem e compare contextos, recorrências "
                "e diferenças. Os códigos organizam evidências; a interpretação "
                "depende da pergunta de pesquisa."
            )},
            {"question": "Como levo os resultados para a escrita?", "answer": (
                "Use os trechos e memos como apoio para construir argumentos, sempre "
                "indicando a fonte e justificando suas escolhas interpretativas. "
                "O Excel exportado facilita a revisão fora da plataforma."
            )},
        ],
    },
    "fallback": {
        "intro": (
            "Posso ajudar você a entender esta área do Análysis e localizar "
            "as ferramentas mais adequadas."
        ),
        "suggestions": [
            {"question": "O que posso fazer nesta página?", "answer": (
                "Esta área faz parte da plataforma Análysis. Observe o título e os "
                "controles visíveis para identificar sua etapa atual; não realizo ações por você."
            )},
            {"question": "Como esta área se relaciona ao meu projeto?", "answer": (
                "Projetos organizam documentos e bases conforme a modalidade. "
                "Volte à lista de projetos da ferramenta para conferir o contexto."
            )},
            {"question": "Onde encontro as principais ferramentas?", "answer": (
                "Use o menu da plataforma para acessar Busca por termos, Busca "
                "estruturada e Análise quali-dados, conforme suas permissões."
            )},
            {"question": "Como volto ao meu projeto?", "answer": (
                "Procure o link Voltar ao projeto quando estiver disponível nesta tela "
                "ou abra a lista de projetos pelo menu da modalidade."
            )},
            {"question": "Onde consulto os resultados?", "answer": (
                "Nas ferramentas de busca, abra as bases de análise do projeto. Na "
                "Quali-dados, use o leitor, o Explorador e o Relatório de codificação."
            )},
        ],
    },
}


def assistant_context_for_endpoint(endpoint: str | None, view_args: dict | None = None) -> dict[str, Any]:
    """Escolhe o contexto pelo endpoint Flask, nunca por parsing da URL no browser."""
    endpoint = endpoint or ""
    if endpoint == "home":
        key = "home"
    elif endpoint == "qualitative.coding_report":
        key = "coding_report"
    elif endpoint.startswith("qualitative."):
        key = "qualitative"
    elif endpoint in {"inicio", "resultado", "progresso", "legacy_free_submit"}:
        key = "term_search"
    elif endpoint.startswith(("historico_racial.", "user_libraries.")):
        key = "structured_search"
    elif endpoint.startswith("projects."):
        key = "projects"
    else:
        key = "fallback"

    args = view_args or {}
    return {
        "key": key,
        "intro": ASSISTANT_CONTEXTS[key]["intro"],
        "suggestions": ASSISTANT_CONTEXTS[key]["suggestions"],
        "page": endpoint,
        "tool": {"term_search": "pdf_scraper", "structured_search": "document_analysis",
                 "qualitative": "qualitative_analysis", "coding_report": "qualitative_analysis"}.get(key),
        "reference": {name: str(args[name]) for name in ("project_id", "analysis_id") if args.get(name)},
    }


def _summary_phrase(project_context: dict[str, Any]) -> str:
    corpus = project_context["corpus"]
    operations = project_context["operations"]
    selected = project_context["analysis"].get("selected")
    records = corpus["analysis_document_records_total"]
    selected_documents = corpus.get("selected_analysis_document_count")
    if project_context["tool"]["id"] == "qualitative_analysis":
        if not selected:
            return f"{records} registro(s) de documento distribuído(s) entre as análises registradas; nenhuma Base está selecionada nesta página."
        codes = operations.get("codes", {}).get("total", 0)
        codings = operations.get("codings", {}).get("total", 0)
        memos = operations.get("memos", {}).get("total", 0)
        return f"{selected_documents} documento(s) na Base selecionada, {codes} código(s), {codings} codificação(ões) e {memos} memo(s)."
    analyses = project_context["analysis"]["project_analyses"]["total"]
    return f"{records} registro(s) de documento em {analyses} base(s) de análise do projeto (registros podem se repetir entre Bases)."


def _origins_phrase(project_context: dict[str, Any]) -> str:
    if not project_context["analysis"].get("selected"):
        return "Nenhuma Base está selecionada nesta página; não atribuo origens de codificação a uma Base atual por fallback."
    origins = project_context["operations"].get("codings", {}).get("origins", {})
    if not origins:
        return "Não há codificações registradas nesta Base."
    listed = "; ".join(f"{origin}: {count}" for origin, count in sorted(origins.items()))
    semantic = " Há registro de automatic_semantic." if "automatic_semantic" in origins else (
        " Não há registro de automatic_semantic nesta Base."
    )
    return f"Dado observado — origens de codificação: {listed}.{semantic}"


def _methodology_draft(project_context: dict[str, Any]) -> str:
    project = project_context["project"]
    summary = _summary_phrase(project_context)
    if project_context["tool"]["id"] == "qualitative_analysis":
        if not project_context["analysis"].get("selected"):
            return (
                f"Dado observado — {summary} Recomendação — abra uma Base específica para que a redação "
                "metodológica use os códigos, memos e origens daquela análise, sem supor que a Base mais recente é a atual."
            )
        origins = project_context["operations"].get("codings", {}).get("origins", {})
        origin_text = ", ".join(sorted(origins)) or "nenhuma origem de codificação"
        recorded_strategy = project_context["operations"].get("recorded_strategy")
        strategy = f" A estratégia registrada foi {recorded_strategy}." if recorded_strategy else ""
        return (
            f"Proposta baseada somente nos registros: “No projeto {project['name']}, a análise quali-dados reuniu {summary.lower()} "
            f"As codificações registradas tiveram as origens {origin_text}.{strategy}” "
            "Complete com critérios de seleção, decisões interpretativas e justificativas que a plataforma não registra."
        )
    return (
        f"Proposta baseada somente nos registros: “No projeto {project['name']}, foram registradas {summary.lower()}” "
        "Complete a redação com critérios de composição do corpus e justificativas metodológicas não registradas pela plataforma."
    )


def _contextual_suggestions(key: str, project_context: dict[str, Any]) -> list[dict[str, str]]:
    """Cinco sugestões locais, atualizadas a cada renderização do projeto."""
    summary = _summary_phrase(project_context)
    tool_id = project_context["tool"]["id"]
    if tool_id == "qualitative_analysis":
        codes = project_context["operations"].get("codes", {})
        listed_codes = codes.get("items", [])
        distribution = "; ".join(
            f"{item['name']}: {item['coding_count']}" for item in listed_codes[:5]
        ) or ("Nenhuma Base está selecionada nesta página."
              if not project_context["analysis"].get("selected")
              else "Ainda não há códigos para distribuir.")
        return [
            {"question": "O que já foi realizado neste projeto?", "answer": f"Dado observado — {summary}"},
            {"question": "Como posso descrever metodologicamente esta análise?", "answer": _methodology_draft(project_context)},
            {"question": "Que tipos de codificação aparecem neste projeto?", "answer": _origins_phrase(project_context)},
            {"question": "Como os códigos estão distribuídos?", "answer": (
                f"Dado observado — contagens por código (frequência, não importância analítica): {distribution}"
            )},
            {"question": "O que ainda preciso explicitar na metodologia?", "answer": (
                "Recomendação — complemente os registros técnicos com critérios de seleção do corpus, "
                "decisões interpretativas e a justificativa das escolhas; esses elementos não são inferidos pela plataforma."
            )},
        ]
    if tool_id == "pdf_scraper":
        searches = project_context["operations"].get("term_searches", {}).get("items", [])
        methods = ", ".join(sorted({item.get("method_used") for item in searches if item.get("method_used")}))
        return [
            {"question": "O que já foi realizado neste projeto?", "answer": f"Dado observado — {summary}"},
            {"question": "Quais termos e métodos aparecem nas bases?", "answer": (
                f"Dado observado — métodos registrados: {methods or 'nenhum método registrado'}. "
                "As listas de termos ficam resumidas por Base no contexto do projeto."
            )},
            {"question": "Como posso descrever metodologicamente as buscas?", "answer": _methodology_draft(project_context)},
            {"question": "O que os resultados permitem afirmar?", "answer": (
                "Recomendação — trate ocorrências persistidas como resultados de localização e revise-as no PDF de origem antes de interpretá-las."
            )},
            {"question": "O que ainda preciso explicitar na metodologia?", "answer": (
                "Recomendação — registre critérios de composição do corpus, seleção de termos e revisão dos resultados; essas justificativas não são comprovadas apenas pelos metadados."
            )},
        ]
    if tool_id == "document_analysis":
        libraries = project_context["operations"].get("associated_libraries", {})
        library_names = ", ".join(item["name"] for item in libraries.get("items", [])) or "nenhuma biblioteca listada"
        return [
            {"question": "O que já foi realizado neste projeto?", "answer": f"Dado observado — {summary}"},
            {"question": "Quais bibliotecas orientaram as buscas?", "answer": f"Dado observado — bibliotecas associadas: {library_names}."},
            {"question": "Quais métodos foram registrados?", "answer": (
                "Dado observado — cada Base preserva o método e as opções disponíveis em seus metadados; "
                "a presença de uma opção na interface não prova que ela foi usada."
            )},
            {"question": "Como posso descrever metodologicamente as buscas?", "answer": _methodology_draft(project_context)},
            {"question": "O que ainda preciso explicitar na metodologia?", "answer": (
                "Recomendação — complemente os registros com critérios de seleção do corpus, escolha da biblioteca e revisão das ocorrências."
            )},
        ]
    return ASSISTANT_CONTEXTS[key]["suggestions"]


def assistant_context_for_request(
    endpoint: str | None,
    view_args: dict | None,
    user: object,
    *,
    query_project_id: object = None,
) -> dict[str, Any]:
    """Acrescenta fatos autorizados ao contexto estático sem fragilizar a página.

    Esta função é usada na renderização. A rota de pergunta reconstrói o
    contexto no servidor, portanto um payload antigo do navegador nunca autoriza
    nem reaproveita o contexto de outro projeto.
    """
    base = assistant_context_for_endpoint(endpoint, view_args)
    reference = dict(base["reference"])
    if "project_id" not in reference and query_project_id:
        reference["project_id"] = str(query_project_id)
    project_context = resolve_project_context(user, reference)
    if project_context is None:
        return base
    actual_tool = project_context["tool"]["id"]
    if base["tool"] and base["tool"] != actual_tool:
        return base
    key = base["key"]
    if key in {"projects", "fallback"}:
        key = TOOL_TO_CONTEXT.get(actual_tool, key)
        base["key"] = key
        base["tool"] = actual_tool
    base["reference"] = {
        "project_id": project_context["project"]["id"],
        **({"analysis_id": project_context["analysis"]["selected"]["id"]}
           if project_context["analysis"]["selected"] else {}),
    }
    base["project_context"] = project_context
    base["context_indicator"] = (
        f"Contexto: {project_context['project']['name']} · {project_context['tool']['label']}"
    )
    if context_has_observed_records(project_context):
        base["intro"] = "Uso dados registrados neste projeto para orientar a leitura; não executo alterações."
        base["suggestions"] = _contextual_suggestions(key, project_context)
    return base
