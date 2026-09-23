import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const python = process.env.PYTHON || (process.platform === 'win32' && await fs.stat('.venv/Scripts/python.exe').then(() => '.venv/Scripts/python.exe').catch(() => false)) || 'python3';
const seedCall = spawnSync(python, ['-c', 'import json; from historico_racial.dictionaries import carregar_entidades; print(json.dumps(carregar_entidades(), ensure_ascii=False))'], { encoding: 'utf8', cwd: path.resolve('.') });
if (seedCall.status !== 0) throw new Error(`Não foi possível ler entities.yml: ${seedCall.stderr}`);
const seed = JSON.parse(seedCall.stdout);
if (Object.keys(seed.grupos).length !== 8 || seed.entidades.length !== 75 || seed.entidades.reduce((sum, entity) => sum + entity.variantes.length, 0) !== 117) {
  throw new Error('O seed oficial não possui as contagens esperadas 8/75/117.');
}

const output = path.resolve('resources/modelo_biblioteca.xlsx');
const workbook = Workbook.create();
const ink = '#183142';
const blue = '#1F4E78';
const paper = '#EAF2F8';

function makeSheet(name, headers, widths) {
  const sheet = workbook.worksheets.add(name);
  const end = String.fromCharCode(64 + headers.length);
  const head = sheet.getRange(`A1:${end}1`);
  head.values = [headers];
  head.format = { fill: blue, font: { name: 'Aptos', size: 11, bold: true, color: '#FFFFFF' }, rowHeight: 26 };
  sheet.getRange(`A2:${end}30`).format.font = { name: 'Aptos', size: 11, color: ink };
  widths.forEach((width, index) => {
    const column = String.fromCharCode(65 + index);
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  });
  return sheet;
}

const instructions = makeSheet('INSTRUÇÕES', ['Etapa', 'Orientação'], [34, 94]);
instructions.getRange('A2:B11').values = [
  ['1. Cópia de referência', 'O conteúdo preenchido reproduz a biblioteca Relações raciais somente como modelo editável. Alterar este arquivo NÃO modifica a biblioteca oficial.'],
  ['2. Grupos', 'Cada grupo recebe um id_grupo estável, único e sem espaços; nome é o rótulo visível.'],
  ['3. Entidades', 'Cada identificador_entidade identifica uma entidade única; informe grupos por seus IDs, separados por ponto e vírgula (;).'],
  ['4. Forma canônica', 'É o rótulo principal escolhido para a entidade, não uma forma “correta”. Inclua-a também em VARIANTES.'],
  ['5. Variantes', 'Repita somente o identificador_entidade, uma variante por linha. Não repita os demais metadados da entidade.'],
  ['6. Multigrupo', 'Uma entidade pode estar em vários grupos (ex.: grupo_a;grupo_b) sem ser duplicada.'],
  ['7. Ativo', 'Informe Sim ou Não para cada grupo, entidade e variante.'],
  ['8. Tipos', 'tipo_entidade: autor, movimento, conceito, obra, categoria_racial, pais_regiao, tradicao, termo_contextual ou outro.'],
  ['9. Metadados', 'tradição intelectual, país/região e observações são opcionais.'],
  ['10. Nova biblioteca', 'Edite o nome em BIBLIOTECA, remova ou altere grupos, entidades e variantes conforme sua pesquisa. A importação criará um novo rascunho, nunca substituirá Relações raciais.'],
];
instructions.getRange('A2:B11').format.wrapText = true;
instructions.getRange('A2:B11').format.rowHeight = 45;
instructions.getRange('A2:A11').format.fill = paper;
instructions.getRange('A2:A11').format.font = { name: 'Aptos', size: 11, bold: true, color: blue };

const library = makeSheet('BIBLIOTECA', ['nome', 'descricao'], [35, 85]);
library.getRange('A2:B2').values = [['Relações raciais — cópia de referência', 'Modelo preenchido a partir do vocabulário oficial. Edite antes de importar como nova biblioteca.']];
const groups = makeSheet('GRUPOS', ['id_grupo', 'nome', 'descricao', 'ativo'], [40, 52, 75, 16]);
groups.getRange(`A2:D${Object.keys(seed.grupos).length + 1}`).values = Object.entries(seed.grupos).map(([id, name]) => [id, name, '', 'Sim']);
groups
  .getRange('D2:D1000').dataValidation = { rule: { type: 'list', values: ['Sim', 'Não'] } };
const entities = makeSheet('ENTIDADES', ['identificador_entidade', 'forma_canonica', 'tipo_entidade', 'grupos', 'ativo', 'tradicao_intelectual', 'pais_regiao', 'observacoes'],
          [28, 40, 22, 42, 15, 30, 28, 70]);
entities.getRange(`A2:H${seed.entidades.length + 1}`).values = seed.entidades.map((entity) => [
  entity.id_entidade, entity.forma_canonica, entity.tipo_entidade,
  Array.isArray(entity.grupo) ? entity.grupo.join(';') : entity.grupo,
  'Sim', entity.tradicao_intelectual || '', entity.pais_regiao || '', entity.observacoes || '',
]);
entities
  .getRange('E2:E1000').dataValidation = { rule: { type: 'list', values: ['Sim', 'Não'] } };
const variantRows = seed.entidades.flatMap((entity) => entity.variantes.map((variant) => [entity.id_entidade, variant, 'Sim']));
const variants = makeSheet('VARIANTES', ['identificador_entidade', 'variante', 'ativo'], [28, 55, 16]);
variants.getRange(`A2:C${variantRows.length + 1}`).values = variantRows;
variants
  .getRange('C2:C1000').dataValidation = { rule: { type: 'list', values: ['Sim', 'Não'] } };

workbook.recalculate();
console.log((await workbook.inspect({ kind: 'sheet', include: 'id,name', maxChars: 2000 })).ndjson);
await fs.mkdir(path.dirname(output), { recursive: true });
await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
console.log(output);
