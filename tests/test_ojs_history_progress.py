"""Exercita o polling silencioso do histórico OJS com estado persistido simulado."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static/js" / "ojs_history_progress.js").read_text(encoding="utf-8")


class OjsHistoryProgressJavascriptTests(unittest.TestCase):
    def run_js(self, checks: str, *, dom: str | None = None) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run(
            [node, "-e", (dom or DOM) + SOURCE + checks],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_polling_updates_persisted_coverage_then_reloads_once_at_completion(self):
        self.run_js(r"""
(async () => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  assert.equal(fetchCalls, 1);
  assert.equal(percentageElement.textContent, '50% verificado nesta referência');
  assert.equal(segments.verified.style.width, '50%');
  assert.equal(segments.reused.style.width, '0%');
  assert.equal(segments.unverified.style.width, '25%');
  assert.equal(segments.unprocessed.style.width, '25%');
  assert.equal(values.verified.textContent, '2');
  assert.equal(values.reused.textContent, '0');
  assert.equal(values.unverified.textContent, '1');
  assert.equal(values.unprocessed.textContent, '1');
  assert.equal(values.eligible.textContent, '4');
  assert.match(statusElement.textContent, /Analisando páginas/);
  assert.match(campaignElement.textContent, /3 lote/);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 5000);
  timers.shift().callback();
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  assert.equal(fetchCalls, 2);
  assert.equal(reloads, 1);
  assert.equal(root.dataset.progressActive, 'false');
  assert.equal(percentageElement.textContent, '100% verificado nesta referência');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")

    def test_source_keeps_backoff_visibility_and_single_request_guards(self):
        self.assertIn("let inFlight = false;", SOURCE)
        self.assertIn("if (!active || inFlight || document.visibilityState === 'hidden') return;", SOURCE)
        self.assertIn("Math.min(maxBackoff, pollInterval * (2 ** Math.min(retryCount, 3)))", SOURCE)
        self.assertIn("document.addEventListener('visibilitychange'", SOURCE)
        self.assertIn("cache: 'no-store'", SOURCE)
        self.assertNotIn("setInterval", SOURCE)
        self.assertNotIn("PlatformFormLoadingOverlay", SOURCE)

    def test_transient_polling_failure_keeps_existing_indicators_and_uses_backoff(self):
        self.run_js(r"""
(async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
  assert.equal(fetchCalls, 1);
  assert.equal(root.dataset.progressActive, 'true');
  assert.equal(reloads, 0);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 10000);
  assert.equal(percentageElement.textContent, 'resultado anterior');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""", dom=DOM + r"""
percentageElement.textContent = 'resultado anterior';
window.fetch = async () => { fetchCalls += 1; throw new Error('temporary_network_failure'); };
""")

    def test_oai_to_html_transition_requests_one_history_refresh(self):
        self.run_js(r"""
(async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
  assert.equal(fetchCalls, 1);
  assert.equal(reloads, 1);
  assert.equal(root.dataset.progressActive, 'false');
  assert.equal(timers.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""", dom=DOM + r"""
root.dataset.progressStage = 'oai_running';
payloads.splice(0, payloads.length, {
  monitoring: {active: true, stage: 'html_pending', label: 'Aguardando análise das páginas', message: 'A campanha HTML foi registrada.'},
  campaign: {status_label: 'Aguardando processamento', batch_count: 1},
  coverage: {eligible_article_count: 4, verified_article_count: 0, reused_evidence_article_count: 0, unverified_article_count: 0, unprocessed_article_count: 4, verified_percentage: 0, available_coverage_percentage: 0},
});
""")


DOM = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(dataset = {}) { this.dataset = {...dataset}; this.style = {}; this.attrs = {}; this.hidden = false; this.textContent = ''; }
  setAttribute(name, value) { this.attrs[name] = value; }
}
const root = new Element({progressUrl: '/progress', progressActive: 'true', progressStage: 'html_running'});
const statusElement = new Element();
const campaignElement = new Element();
const percentageElement = new Element();
const progressElement = new Element();
const segments = {verified: new Element({ojsCoverageSegment: 'verified'}), reused: new Element({ojsCoverageSegment: 'reused'}), unverified: new Element({ojsCoverageSegment: 'unverified'}), unprocessed: new Element({ojsCoverageSegment: 'unprocessed'})};
const values = {verified: new Element({ojsCoverageValue: 'verified'}), reused: new Element({ojsCoverageValue: 'reused'}), unverified: new Element({ojsCoverageValue: 'unverified'}), unprocessed: new Element({ojsCoverageValue: 'unprocessed'}), eligible: new Element({ojsCoverageValue: 'eligible'})};
const selector = {
  '[data-ojs-history-progress]': root,
  '[data-ojs-progress-status]': statusElement,
  '[data-ojs-progress-campaign-status]': campaignElement,
  '[data-ojs-coverage-percentage]': percentageElement,
  '[data-ojs-coverage-progress]': progressElement,
};
const document = {
  visibilityState: 'visible', listeners: {},
  querySelector: (value) => selector[value] || null,
  querySelectorAll: (value) => value === '[data-ojs-coverage-segment]' ? Object.values(segments) : value === '[data-ojs-coverage-value]' ? Object.values(values) : [],
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
};
const timers = [];
let reloads = 0;
let fetchCalls = 0;
const payloads = [
  {monitoring: {active: true, stage: 'html_running', label: 'Analisando páginas dos artigos', message: 'O worker HTML está analisando as páginas dos artigos em lotes seguros.'}, campaign: {status_label: 'Em andamento', batch_count: 3}, coverage: {eligible_article_count: 4, verified_article_count: 2, reused_evidence_article_count: 0, unverified_article_count: 1, unprocessed_article_count: 1, verified_percentage: 50, available_coverage_percentage: 50}},
  {monitoring: {active: false, stage: null, label: null, message: null}, campaign: {status_label: 'Concluída', batch_count: 4}, coverage: {eligible_article_count: 4, verified_article_count: 4, reused_evidence_article_count: 0, unverified_article_count: 0, unprocessed_article_count: 0, verified_percentage: 100, available_coverage_percentage: 100}},
];
const window = {
  fetch: async () => { fetchCalls += 1; return {ok: true, status: 200, json: async () => payloads.shift()}; },
  setTimeout(callback, delay) { timers.push({callback, delay}); return timers.length; },
  clearTimeout() {}, addEventListener() {}, location: {reload: () => { reloads += 1; }},
};
"""


if __name__ == "__main__":
    unittest.main()
