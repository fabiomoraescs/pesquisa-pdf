"""Contrato do salvamento assíncrono da visibilidade, sem navegador real."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js/admin_tool_visibility.js").read_text(encoding="utf-8")


class ToolVisibilityFrontendTests(unittest.TestCase):
    def test_switch_saves_without_navigation_prevents_duplicates_and_restores_on_failure(self):
        self.assertNotIn("location.href", SOURCE)
        self.assertNotIn("data-tool-visibility-state", SOURCE)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js não está disponível para validar o contrato do switch.")
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
class E {
  constructor() {
    this.listeners = {}; this.dataset = {}; this.classNames = new Set(); this.classList = {
      toggle: (name, enabled) => { if (enabled) this.classNames.add(name); else this.classNames.delete(name); },
    };
    this.checked = false; this.disabled = false; this.hidden = false; this.textContent = ''; this.attrs = {};
  }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  fire(type, event = {}) { return this.listeners[type](event); }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
}
const form = new E(), input = new E(), feedback = new E(), badge = new E(), article = new E(), children = new E(), refreshButton = new E();
feedback.hidden = true;
input.checked = true;
input.value = 'true';
input.dataset = {visibilityKind: 'group', visibilityId: 'analytics', visibilityCurrent: 'true'};
const csrf = {value: 'csrf-test-only'};
form.action = '/admin/visibilidade/group/analytics';
form.querySelector = (selector) => ({
  '[data-tool-visibility-control]': input,
  '[name=csrf_token]': csrf,
  '[data-tool-visibility-feedback]': feedback,
}[selector] || null);
form.closest = (selector) => selector === 'article' ? article : null;
article.querySelector = (selector) => selector === '[data-tool-visibility-badge]' ? badge : null;
const childInput = new E();
childInput.checked = true;
const document = {
  querySelectorAll: (selector) => selector === '[data-tool-visibility-form]' ? [form] : [],
  querySelector: (selector) => ({
    '[data-tool-visibility-refresh]': refreshButton,
    '[data-tool-visibility-children="analytics"]': children,
  }[selector] || null),
};
class FormData {
  constructor() { this.values = {csrf_token: csrf.value}; }
  set(key, value) { this.values[key] = value; }
  get(key) { return this.values[key]; }
}
let calls = 0, resolveRequest, rejectRequest, request;
const fetch = (url, options) => {
  calls += 1; request = {url, options};
  return new Promise((resolve, reject) => { resolveRequest = resolve; rejectRequest = reject; });
};
vm.runInNewContext(SOURCE, {document, fetch, FormData});
(async () => {
  input.checked = false;
  const saving = input.fire('change');
  assert.equal(calls, 1);
  assert.equal(input.disabled, true);
  assert.equal(refreshButton.disabled, true);
  assert.equal(refreshButton.attrs['aria-busy'], 'true');
  assert.equal(form.attrs['aria-busy'], 'true');
  assert.equal(feedback.textContent, 'Salvando visibilidade…');
  assert.equal(feedback.hidden, true);
  assert.equal(request.url, '/admin/visibilidade/group/analytics');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal(request.options.headers.Accept, 'application/json');
  assert.equal(request.options.headers['X-Requested-With'], 'XMLHttpRequest');
  assert.equal(request.options.headers['X-CSRFToken'], 'csrf-test-only');
  assert.equal(request.options.body.get('visible'), 'false');
  input.checked = true;
  input.fire('change');
  assert.equal(calls, 1);
  resolveRequest({ok: true, json: async () => ({ok:true, changed:true, visible:false})});
  await saving;
  assert.equal(input.checked, false);
  assert.equal(input.dataset.visibilityCurrent, 'false');
  assert.equal(badge.textContent, 'Oculta');
  assert.ok(badge.classNames.has('platform-badge-inactive'));
  assert.equal(children.hidden, true);
  assert.equal(childInput.checked, true);
  assert.equal(input.disabled, false);
  assert.equal(refreshButton.disabled, false);
  assert.equal(refreshButton.attrs['aria-busy'], 'false');
  assert.equal(form.attrs['aria-busy'], undefined);

  input.checked = true;
  const failing = input.fire('change');
  assert.equal(calls, 2);
  resolveRequest({ok: false, json: async () => ({ok:false, message:'Falha segura'})});
  await failing;
  assert.equal(input.checked, false);
  assert.equal(input.dataset.visibilityCurrent, 'false');
  assert.equal(children.hidden, true);
  assert.match(feedback.textContent, /estado anterior foi preservado/);
  assert.equal(feedback.hidden, false);
  assert.equal(input.disabled, false);

  input.checked = true;
  const reopening = input.fire('change');
  assert.equal(calls, 3);
  resolveRequest({ok: true, json: async () => ({ok:true, changed:true, visible:true})});
  await reopening;
  assert.equal(input.checked, true);
  assert.equal(badge.textContent, 'Visível');
  assert.equal(children.hidden, false);
  assert.equal(childInput.checked, true);

  input.checked = false;
  const uncertain = input.fire('change');
  assert.equal(calls, 4);
  rejectRequest(new Error('offline'));
  await uncertain;
  assert.equal(input.checked, true);
  assert.equal(children.hidden, false);
  assert.match(feedback.textContent, /Não foi possível confirmar a atualização/);
  assert.equal(feedback.hidden, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        script = harness.replace("SOURCE", repr(SOURCE))
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_refresh_reloads_the_current_view_without_submitting_and_restores_scroll(self):
        self.assertIn('refreshButton.addEventListener("click"', SOURCE)
        self.assertIn("window.location.reload()", SOURCE)
        self.assertIn("pendingSaves > 0", SOURCE)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js não está disponível para validar o controle Atualizar.")
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
class E {
  constructor() { this.listeners = {}; this.attrs = {}; this.disabled = false; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  fire(type) { return this.listeners[type](); }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
}
const refreshButton = new E();
const storage = new Map([['analysis:tool-visibility-refresh-scroll', '222']]);
let restored = null, reloads = 0;
const document = {
  querySelectorAll: () => [],
  querySelector: (selector) => selector === '[data-tool-visibility-refresh]' ? refreshButton : null,
};
const sessionStorage = {
  getItem: (key) => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, value),
  removeItem: (key) => storage.delete(key),
};
const window = {
  scrollY: 333,
  requestAnimationFrame: (callback) => callback(),
  scrollTo: (options) => { restored = options; },
  location: { reload: () => { reloads += 1; } },
};
vm.runInNewContext(SOURCE, {document, sessionStorage, window});
assert.equal(restored.top, 222);
assert.equal(restored.behavior, 'auto');
assert.equal(storage.has('analysis:tool-visibility-refresh-scroll'), false);
refreshButton.fire('click');
assert.equal(storage.get('analysis:tool-visibility-refresh-scroll'), '333');
assert.equal(refreshButton.disabled, true);
assert.equal(refreshButton.attrs['aria-busy'], 'true');
assert.equal(reloads, 1);
"""
        script = harness.replace("SOURCE", repr(SOURCE))
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
