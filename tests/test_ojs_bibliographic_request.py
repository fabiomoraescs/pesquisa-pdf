"""Verifica o envio assíncrono OJS sem exigir navegador ou coleta externa."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js" / "ojs_bibliographic_request.js").read_text(encoding="utf-8")


class OjsBibliographicRequestJavascriptTests(unittest.TestCase):
    def test_source_uses_persisted_status_and_the_shared_overlay(self):
        self.assertIn("data-ojs-bibliographic-request", SOURCE)
        self.assertIn("PlatformFormLoadingOverlay", SOURCE)
        self.assertIn("const pollInterval = 5000;", SOURCE)
        self.assertIn("X-Requested-With", SOURCE)
        self.assertIn("credentials: 'same-origin'", SOURCE)
        self.assertIn("cache: 'no-store'", SOURCE)
        self.assertIn("window.location.reload()", SOURCE)
        self.assertIn("nunca executa workers", SOURCE)
        self.assertNotIn("setInterval", SOURCE)
        self.assertNotIn("0%", SOURCE)

    def test_submission_is_single_and_keeps_the_shared_overlay_dismissible(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run(
            [node, "-e", DOM + SOURCE + CHECKS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


DOM = r"""
const assert = require('node:assert/strict');
class ClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
}
class Element {
  constructor(dataset = {}) { this.dataset = {...dataset}; this.hidden = true; this.textContent = ''; this.attrs = {}; this.classList = new ClassList(); this.listeners = {}; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
const root = new Element({progressUrl: '/progress', ojsJournalName: 'Revista Latitude'});
const feedback = new Element();
const feedbackStatus = new Element();
const overlay = new Element();
const title = new Element();
const context = new Element();
const message = new Element();
const detail = new Element();
const close = new Element();
const submit = new Element();
const form = new Element({loadingOverlayTarget: 'ojs-seo-overlay-processamento'});
form.checkValidity = () => true;
form.querySelectorAll = () => [submit];
const selectors = {
  '[data-ojs-history-progress]': root,
  '[data-ojs-async-progress]': feedback,
  '[data-ojs-async-progress-status]': feedbackStatus,
};
const ids = {
  'ojs-seo-overlay-processamento': overlay,
  'ojs-seo-titulo-processamento': title,
  'ojs-seo-contexto-processamento': context,
  'ojs-seo-mensagem-processamento': message,
  'ojs-seo-detalhe-processamento': detail,
  'ojs-seo-fechar-processamento': close,
};
const document = {
  visibilityState: 'visible', body: {classList: new ClassList()},
  querySelector: (value) => selectors[value] || null,
  querySelectorAll: (value) => value === '[data-ojs-bibliographic-request]' ? [form] : [],
  getElementById: (value) => ids[value] || null,
  addEventListener() {},
};
const timers = [];
const requests = [];
let terminal = false;
let reloads = 0;
const window = {
  PlatformFormLoadingOverlay: {
    show: () => { overlay.hidden = false; document.body.classList.add('is-processing'); },
    hide: () => { overlay.hidden = true; document.body.classList.remove('is-processing'); },
    reset: () => { overlay.hidden = true; submit.disabled = false; document.body.classList.remove('is-processing'); },
  },
  fetch: async (url, options = {}) => {
    requests.push({url, options});
    if (options.method === 'POST') return {status: 202, json: async () => ({stage: 'oai_pending', progress_url: '/progress', message: 'A solicitação foi registrada e aguarda o worker dos dados bibliográficos.'})};
    return {ok: true, status: 200, json: async () => terminal
      ? ({monitoring: {active: false}, audit: {status: 'failed', errors: [{message: 'Endpoint OAI-PMH indisponível.'}]}})
      : ({monitoring: {active: true, label: 'Aguardando processamento', message: 'A análise dos dados bibliográficos aguarda o worker.'}})};
  },
  setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
  clearTimeout() {}, location: {reload() { reloads += 1; }},
};
const FormData = class { constructor(source) { this.source = source; } };
"""

CHECKS = r"""
(async () => {
  form.listeners.submit({preventDefault() {}});
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(requests[0].options.headers['X-Requested-With'], 'XMLHttpRequest');
  assert.equal(requests[0].options.headers.Accept, 'application/json');
  assert.equal(root.dataset.progressActive, 'true');
  assert.equal(title.textContent, 'Aguardando processamento');
  assert.match(message.textContent, /aguarda o worker/);
  assert.equal(context.textContent, 'Revista Latitude');
  assert.equal(close.hidden, false);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 5000);
  form.listeners.submit({preventDefault() {}});
  for (let index = 0; index < 2; index += 1) await Promise.resolve();
  assert.equal(requests.length, 2);
  close.listeners.click();
  assert.equal(overlay.hidden, true);
  assert.equal(document.body.classList.contains('is-processing'), false);
  terminal = true;
  timers.shift().callback();
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
  assert.match(feedbackStatus.textContent, /Não foi possível concluir a análise/);
  assert.match(feedbackStatus.textContent, /Endpoint OAI-PMH indisponível/);
  assert.equal(feedback.classList.contains('alert-danger'), true);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 750);
  timers[0].callback();
  assert.equal(reloads, 1);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


if __name__ == "__main__":
    unittest.main()
