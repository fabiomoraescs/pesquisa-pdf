"""Contrato do AJAX do painel de IA sem navegador ou chamada externa."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/admin_ai_settings.js").read_text(encoding="utf-8")


class AdminAISettingsFrontendTests(unittest.TestCase):
    def test_connection_test_updates_its_card_without_navigation(self):
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
class E {
  constructor() { this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.textContent = ''; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  fire(type, event = {}) { return this.listeners[type](event); }
}
const form = new E(), strategy = new E(), primary = new E(), fallback = new E(), csrf = new E();
const enabled = new E(), enabledBadge = new E(), primaryBadge = new E(), button = new E(), status = new E();
strategy.value = 'single'; primary.value = 'gemini'; primary.options = []; primary.selectedOptions = [{disabled:false}];
enabled.checked = true; enabled.value = 'gemini'; enabled.dataset.aiEnabled = 'gemini';
enabledBadge.dataset.aiEnabledBadge = 'gemini'; primaryBadge.dataset.aiPrimaryBadge = 'gemini';
button.dataset.aiConnectionTest = 'gemini'; button.formAction = '/admin/inteligencia-artificial/gemini/testar-conexao'; button.textContent = 'Testar conexão';
csrf.value = 'csrf-test-only'; status.dataset.aiConnectionState = 'not_tested';
form.querySelector = (selector) => ({'[data-ai-strategy]': strategy, '[data-ai-primary]': primary, '[data-ai-fallback]': fallback, '[name=csrf_token]': csrf, '[data-ai-connection-status="gemini"]': status}[selector]);
form.querySelectorAll = (selector) => ({'[data-ai-enabled]': [enabled], '[data-ai-fallback-provider]': [], '[data-ai-enabled-badge]': [enabledBadge], '[data-ai-primary-badge]': [primaryBadge], '[data-ai-connection-test]': [button]}[selector] || []);
const document = {querySelector: (selector) => selector === '[data-ai-settings-form]' ? form : null};
let request;
const fetch = async (url, options) => { request = {url, options}; return {ok:true, json: async () => ({ok:true, provider_id:'gemini', connection_state:'available', connection_status:'Conexão disponível'})}; };
vm.runInNewContext(SOURCE, {document, fetch, encodeURIComponent});
(async () => {
  let prevented = false;
  await button.fire('click', {preventDefault(){ prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(request.url, '/admin/inteligencia-artificial/gemini/testar-conexao');
  assert.equal(request.options.headers.Accept, 'application/json');
  assert.equal(request.options.headers['X-Requested-With'], 'XMLHttpRequest');
  assert.match(request.options.body, /csrf_token=/);
  assert.equal(status.textContent, 'Conexão disponível');
  assert.equal(status.dataset.aiConnectionState, 'available');
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Testar conexão');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""".replace("SOURCE", repr(SOURCE))
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
