(() => {
  const dado = JSON.parse(document.getElementById('vocab-data').textContent);
  const categorias = JSON.parse(document.getElementById('vocab-categorias').textContent);
  const rotulos = JSON.parse(document.getElementById('vocab-rotulos').textContent);
  const rotulo = (valor) => rotulos[valor] || valor;
  const dataBrUtc = (valor) => new Date(valor).toLocaleString('pt-BR', {timeZone: 'UTC', dateStyle: 'short', timeStyle: 'short'});
  let baseVersion = dado.version;
  let original = structuredClone(dado.vocabulario);
  let rascunho = structuredClone(original);
  const gruposEl = document.getElementById('vocab-grupos');
  const salvarEl = document.getElementById('vocab-salvar');
  const resumoEl = document.getElementById('vocab-resumo');
  const mensagemEl = document.getElementById('vocab-mensagem');
  const dialog = document.getElementById('vocab-dialog');
  const dialogForm = document.getElementById('vocab-dialog-form');
  const dialogCampos = document.getElementById('vocab-dialog-campos');
  const dialogErro = document.getElementById('vocab-dialog-erro');
  let dialogAction = null;
  const abertos = new Set();

  const escapar = (texto) => String(texto ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const diferiu = () => JSON.stringify(rascunho) !== JSON.stringify(original);
  const achar = (id) => rascunho.entidades.find((entidade) => entidade.id_entidade === id);
  const idSeguro = (texto, existentes) => {
    const base = texto.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 68) || 'item';
    const raiz = /^[a-z]/.test(base) ? base : `item_${base}`;
    let candidato = raiz, indice = 2;
    while (existentes.has(candidato)) candidato = `${raiz}_${indice++}`;
    return candidato;
  };

  function resumo() {
    const gruposCriados = Object.keys(rascunho.grupos).length - Object.keys(original.grupos).length;
    const entidadesAdicionadas = rascunho.entidades.length - original.entidades.length;
    const variantesAntes = original.entidades.reduce((n, e) => n + e.variantes.length, 0);
    const variantesAgora = rascunho.entidades.reduce((n, e) => n + e.variantes.length, 0);
    const variantesAdicionadas = variantesAgora - variantesAntes;
    const estadosAlterados = Object.entries(rascunho.grupos).filter(([id, g]) => original.grupos[id] && original.grupos[id].ativo !== g.ativo).length
      + rascunho.entidades.filter((e) => original.entidades.some((antiga) => antiga.id_entidade === e.id_entidade && antiga.ativo !== e.ativo)).length;
    resumoEl.textContent = diferiu()
      ? `Alterações no rascunho: ${gruposCriados} grupo(s) criado(s), ${entidadesAdicionadas} entidade(s) adicionada(s), ${variantesAdicionadas} variante(s) adicionada(s), ${estadosAlterados} estado(s) alterado(s); demais edições também serão incluídas.`
      : 'Nenhuma alteração pendente.';
    salvarEl.disabled = !diferiu();
    document.getElementById('vocab-contagens').textContent = `${Object.keys(rascunho.grupos).length} grupos · ${rascunho.entidades.length} entidades · ${variantesAgora} variantes`;
  }

  function renderizar() {
    const blocos = Object.entries(rascunho.grupos).map(([id, grupo]) => {
      const entidades = rascunho.entidades.filter((e) => e.grupo.includes(id));
      const cards = entidades.map((e) => {
        const controlesGrupos = Object.entries(rascunho.grupos).map(([gid, g]) =>
          `<label class="hr-vocab-check"><input type="checkbox" data-action="entidade-grupo" data-id="${escapar(e.id_entidade)}" data-gid="${escapar(gid)}" ${e.grupo.includes(gid) ? 'checked' : ''}> ${escapar(g.nome)}</label>`
        ).join('');
        const variantes = e.variantes.map((v, indice) => `
          <div class="hr-vocab-variant">
            <label class="hr-vocab-check"><input type="checkbox" data-action="variante-ativo" data-id="${escapar(e.id_entidade)}" data-index="${indice}" ${v.ativo ? 'checked' : ''}> Ativa</label>
            <input class="form-control form-control-sm" aria-label="Texto da variante" data-action="variante-texto" data-id="${escapar(e.id_entidade)}" data-index="${indice}" value="${escapar(v.texto)}">
          </div>`).join('');
        const tipos = categorias.tipos_entidade.map((tipo) => `<option value="${escapar(tipo)}" ${e.tipo_entidade === tipo ? 'selected' : ''}>${escapar(rotulo(tipo))}</option>`).join('');
        const tradicoes = [''].concat(categorias.tradicoes_intelectuais).map((t) => `<option value="${escapar(t)}" ${e.tradicao_intelectual === t ? 'selected' : ''}>${escapar(t ? rotulo(t) : 'Não informada')}</option>`).join('');
        return `<details class="hr-vocab-entity" data-open-key="entidade:${escapar(e.id_entidade)}" ${abertos.has(`entidade:${e.id_entidade}`) ? 'open' : ''}>
          <summary><span>${escapar(e.forma_canonica)}</span><small>${escapar(rotulo(e.tipo_entidade))} · ${e.variantes.length} variante(s) · ${e.ativo ? 'Ativa' : 'Inativa'}</small></summary>
          <div class="hr-vocab-entity-body">
            <label class="hr-vocab-check"><input type="checkbox" data-action="entidade-ativo" data-id="${escapar(e.id_entidade)}" ${e.ativo ? 'checked' : ''}> Entidade ativa</label>
            <div class="hr-vocab-fields"><label>Forma canônica<input class="form-control form-control-sm" data-action="entidade-canonica" data-id="${escapar(e.id_entidade)}" value="${escapar(e.forma_canonica)}"></label>
            <label>Tipo<select class="form-select form-select-sm" data-action="entidade-tipo" data-id="${escapar(e.id_entidade)}">${tipos}</select></label></div>
            <fieldset><legend class="fs-6">Grupos da entidade</legend><div class="hr-vocab-group-options">${controlesGrupos}</div></fieldset>
            <div class="hr-vocab-fields"><label>Tradição intelectual<select class="form-select form-select-sm" data-action="entidade-tradicao" data-id="${escapar(e.id_entidade)}">${tradicoes}</select></label>
            <label>País/região<input class="form-control form-control-sm" data-action="entidade-pais" data-id="${escapar(e.id_entidade)}" value="${escapar(e.pais_regiao)}"></label></div>
            <label class="d-block mt-2">Observações<input class="form-control form-control-sm" data-action="entidade-observacoes" data-id="${escapar(e.id_entidade)}" value="${escapar(e.observacoes)}"></label>
            <details class="mt-2" data-open-key="variantes:${escapar(e.id_entidade)}" ${abertos.has(`variantes:${e.id_entidade}`) ? 'open' : ''}><summary>Ver variantes</summary>${variantes}<button class="btn btn-outline-primary btn-sm mt-2" type="button" data-action="adicionar-variante" data-id="${escapar(e.id_entidade)}">+ Adicionar variante</button></details>
          </div></details>`;
      }).join('');
      return `<details class="hr-vocab-group" data-open-key="grupo:${escapar(id)}" ${abertos.has(`grupo:${id}`) ? 'open' : ''}>
        <summary><strong>${escapar(grupo.nome)}</strong><small>${entidades.length} entidade(s) · ${grupo.ativo ? 'Ativo' : 'Inativo'}</small></summary>
        <div class="hr-vocab-group-body">
          <label class="hr-vocab-check"><input type="checkbox" data-action="grupo-ativo" data-id="${escapar(id)}" ${grupo.ativo ? 'checked' : ''}> Grupo ativo</label>
          <div class="hr-vocab-fields"><label>Nome<input class="form-control form-control-sm" data-action="grupo-nome" data-id="${escapar(id)}" value="${escapar(grupo.nome)}"></label>
          <label>Descrição<input class="form-control form-control-sm" data-action="grupo-descricao" data-id="${escapar(id)}" value="${escapar(grupo.descricao)}"></label></div>
          <div class="hr-vocab-entities">${cards || '<p class="form-text">Nenhuma entidade associada.</p>'}</div>
          <button class="btn btn-outline-primary btn-sm mt-2" type="button" data-action="adicionar-entidade" data-id="${escapar(id)}">+ Adicionar entidade</button>
        </div></details>`;
    });
    gruposEl.innerHTML = blocos.join('');
    resumo();
  }

  function abrirDialog(titulo, campos, aoConfirmar) {
    document.getElementById('vocab-dialog-titulo').textContent = titulo;
    dialogCampos.innerHTML = campos;
    dialogErro.hidden = true;
    dialogAction = aoConfirmar;
    dialog.showModal();
  }

  document.getElementById('vocab-criar-grupo').addEventListener('click', () => {
    abrirDialog('Criar novo grupo', `<label class="form-label d-block">Nome do grupo<input class="form-control" name="nome" required maxlength="160"></label>
      <label class="form-label d-block mt-2">Descrição (opcional)<textarea class="form-control" name="descricao" rows="3"></textarea></label>`, (form) => {
      const nome = form.get('nome').trim();
      if (!nome) throw new Error('Informe o nome do grupo.');
      const id = idSeguro(nome, new Set(Object.keys(rascunho.grupos)));
      rascunho.grupos[id] = {id_grupo: id, nome, descricao: form.get('descricao').trim(), ativo: true};
      abertos.add(`grupo:${id}`);
    });
  });

  function dialogEntidade(grupoPreselecionado) {
    const grupos = Object.entries(rascunho.grupos).map(([id, grupo]) =>
      `<label class="hr-vocab-check"><input type="checkbox" name="grupo" value="${escapar(id)}" ${id === grupoPreselecionado ? 'checked' : ''}> ${escapar(grupo.nome)}</label>`).join('');
    const tipos = categorias.tipos_entidade.map((tipo) => `<option value="${escapar(tipo)}">${escapar(rotulo(tipo))}</option>`).join('');
    const tradicoes = [''].concat(categorias.tradicoes_intelectuais).map((t) => `<option value="${escapar(t)}">${escapar(t ? rotulo(t) : 'Não informada')}</option>`).join('');
    abrirDialog('Adicionar entidade', `<label class="form-label d-block">Forma canônica<input class="form-control" name="canonica" required maxlength="200"></label>
      <fieldset class="mt-3"><legend class="fs-6">Grupo(s)</legend><div class="hr-vocab-group-options">${grupos}</div></fieldset>
      <label class="form-label d-block mt-3">Variantes (uma por linha; a forma canônica será incluída)<textarea class="form-control" name="variantes" rows="4"></textarea></label>
      <label class="form-label d-block mt-2">Tipo<select class="form-select" name="tipo">${tipos}</select></label>
      <label class="form-label d-block mt-2">Tradição intelectual<select class="form-select" name="tradicao">${tradicoes}</select></label>
      <label class="form-label d-block mt-2">País/região (opcional)<input class="form-control" name="pais"></label>`, (form) => {
      const canonica = form.get('canonica').trim();
      const associados = form.getAll('grupo');
      if (!canonica || !associados.length) throw new Error('Informe a forma canônica e ao menos um grupo.');
      const variantes = [canonica, ...form.get('variantes').split(/\r?\n/).map((v) => v.trim()).filter(Boolean)];
      const unicas = [...new Map(variantes.map((v) => [v.toLocaleLowerCase('pt-BR'), v])).values()];
      const id = idSeguro(canonica, new Set(rascunho.entidades.map((e) => e.id_entidade)));
      rascunho.entidades.push({id_entidade: id, forma_canonica: canonica, variantes: unicas.map((texto) => ({texto, ativo: true})),
        tipo_entidade: form.get('tipo'), grupo: associados, tradicao_intelectual: form.get('tradicao'),
        pais_regiao: form.get('pais').trim(), observacoes: '', ativo: true});
      associados.forEach((gid) => abertos.add(`grupo:${gid}`));
      abertos.add(`entidade:${id}`);
    });
  }
  document.getElementById('vocab-criar-entidade').addEventListener('click', () => dialogEntidade(null));

  gruposEl.addEventListener('toggle', (evento) => {
    const key = evento.target.dataset?.openKey;
    if (key) (evento.target.open ? abertos.add(key) : abertos.delete(key));
  }, true);

  gruposEl.addEventListener('click', (evento) => {
    const alvo = evento.target.closest('button[data-action]');
    if (!alvo) return;
    if (alvo.dataset.action === 'adicionar-entidade') dialogEntidade(alvo.dataset.id);
    if (alvo.dataset.action === 'adicionar-variante') {
      const id = alvo.dataset.id;
      abrirDialog(`Adicionar variante a ${achar(id).forma_canonica}`, `<label class="form-label d-block">Texto da variante<input class="form-control" name="texto" required maxlength="200"></label>`, (form) => {
        const texto = form.get('texto').trim();
        if (!texto) throw new Error('Informe a variante.');
        achar(id).variantes.push({texto, ativo: true});
        abertos.add(`variantes:${id}`);
      });
    }
  });

  gruposEl.addEventListener('change', (evento) => {
    const el = evento.target;
    const {action, id, gid, index} = el.dataset;
    if (!action) return;
    if (action === 'grupo-ativo') rascunho.grupos[id].ativo = el.checked;
    if (action === 'grupo-nome') rascunho.grupos[id].nome = el.value.trim();
    if (action === 'grupo-descricao') rascunho.grupos[id].descricao = el.value.trim();
    const entidade = achar(id);
    if (entidade) {
      if (action === 'entidade-ativo') entidade.ativo = el.checked;
      if (action === 'entidade-canonica') {
        entidade.forma_canonica = el.value.trim();
        if (!entidade.variantes.some((v) => v.texto.toLocaleLowerCase('pt-BR') === entidade.forma_canonica.toLocaleLowerCase('pt-BR')))
          entidade.variantes.push({texto: entidade.forma_canonica, ativo: true});
      }
      if (action === 'entidade-tipo') entidade.tipo_entidade = el.value;
      if (action === 'entidade-tradicao') entidade.tradicao_intelectual = el.value;
      if (action === 'entidade-pais') entidade.pais_regiao = el.value.trim();
      if (action === 'entidade-observacoes') entidade.observacoes = el.value.trim();
      if (action === 'entidade-grupo') {
        entidade.grupo = el.checked ? [...new Set([...entidade.grupo, gid])] : entidade.grupo.filter((item) => item !== gid);
      }
      if (action === 'variante-ativo') entidade.variantes[Number(index)].ativo = el.checked;
      if (action === 'variante-texto') entidade.variantes[Number(index)].texto = el.value.trim();
    }
    if (['entidade-grupo', 'entidade-canonica', 'entidade-ativo', 'grupo-nome', 'grupo-ativo'].includes(action)) renderizar();
    else resumo();
  });

  dialogForm.addEventListener('submit', (evento) => {
    evento.preventDefault();
    try {
      dialogAction(new FormData(dialogForm));
      dialog.close();
      renderizar();
    } catch (erro) {
      dialogErro.textContent = erro.message;
      dialogErro.hidden = false;
    }
  });
  document.getElementById('vocab-dialog-cancelar').addEventListener('click', () => dialog.close());

  salvarEl.addEventListener('click', async () => {
    salvarEl.disabled = true;
    mensagemEl.hidden = true;
    try {
      const resposta = await fetch(document.getElementById('vocab-data').dataset.saveUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify({base_version: baseVersion, vocabulario: rascunho, nota: document.getElementById('vocab-nota').value}),
      });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível salvar o vocabulário.');
      baseVersion = dados.version;
      original = structuredClone(rascunho);
      document.getElementById('vocab-versao').textContent = dados.version;
      document.querySelectorAll('#vocab-historico-corpo .badge').forEach((badge) => badge.remove());
      document.getElementById('vocab-historico-corpo').insertAdjacentHTML('afterbegin',
        `<tr><td>${escapar(dados.version)} <span class="badge text-bg-primary">ativa</span></td><td>${escapar(dataBrUtc(dados.created_at))}</td><td>${dados.counts.grupos}</td><td>${dados.counts.entidades}</td><td>${dados.counts.variantes}</td><td>${escapar(dados.note)}</td></tr>`);
      document.getElementById('vocab-nota').value = '';
      mensagemEl.className = 'alert alert-success mt-3';
      mensagemEl.textContent = dados.aviso;
      mensagemEl.hidden = false;
      resumo();
    } catch (erro) {
      mensagemEl.className = 'alert alert-danger mt-3';
      mensagemEl.textContent = erro.message;
      mensagemEl.hidden = false;
      salvarEl.disabled = false;
    }
  });
  window.addEventListener('beforeunload', (evento) => {
    if (diferiu()) { evento.preventDefault(); evento.returnValue = ''; }
  });
  renderizar();
})();
