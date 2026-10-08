from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from platform_core.instagram_action_evaluations import ActionEvaluationPreview, format_metric_value


_DETAIL_HEADER_LAYOUT = "d-flex flex-wrap align-items-start justify-content-between gap-2"
_DETAIL_HEADER_COPY = "flex-grow-1"
_DETAIL_HEADER_ACTIONS = "d-flex flex-wrap align-items-center gap-2 flex-shrink-0"


class _HeaderChildrenParser(HTMLParser):
    """Collect direct children from the shared two-column detail-header wrapper."""

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[str] = []
        self._header_depth: int | None = None
        self._finished = False
        self.children: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        depth = len(self._stack)
        classes = attributes.get("class", "") or ""

        if tag == "div" and classes == _DETAIL_HEADER_LAYOUT and self._header_depth is None:
            self._header_depth = depth
        elif not self._finished and self._header_depth is not None and depth == self._header_depth + 1:
            self.children.append((tag, classes))

        self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag == "div"
            and self._header_depth is not None
            and len(self._stack) == self._header_depth + 1
        ):
            self._finished = True
        if self._stack:
            self._stack.pop()


def _detail_header_children(template: str) -> list[tuple[str, str]]:
    parser = _HeaderChildrenParser()
    parser.feed(template)
    return parser.children


def test_relative_change_formats_decimal_ratio_as_brazilian_percentage():
    absolute_change = 618 - 1560
    relative_change = absolute_change / 1560
    preview = ActionEvaluationPreview(
        action=None,
        analytics_run=None,
        metric_key="saves",
        metric_label="Salvamentos",
        unit="contagem",
        baseline_value=1560,
        followup_value=618,
        absolute_change=absolute_change,
        relative_change=relative_change,
        baseline_sample_size=9,
        baseline_coverage=1,
        followup_sample_size=9,
        followup_population_size=9,
        followup_coverage=1,
        limitations=(),
    )

    assert absolute_change == -942
    assert relative_change == -942 / 1560
    assert preview.relative_display == "-60,38%"
    assert preview.followup_context_display == "n = 9 de 9 · cobertura 100%"


def test_relative_change_percentage_presentation_handles_direction_and_zero():
    options = {"signed": True, "percentage_fraction": True, "fixed_decimals": 2}

    assert format_metric_value((110 - 100) / 100, "%", **options) == "+10,00%"
    assert format_metric_value((90 - 100) / 100, "%", **options) == "-10,00%"
    assert format_metric_value((100 - 100) / 100, "%", **options) == "0,00%"


def test_relative_change_percentage_presentation_keeps_unavailable_values_unavailable():
    assert format_metric_value(
        None,
        "%",
        signed=True,
        percentage_fraction=True,
        fixed_decimals=2,
    ) == "Não disponível"


def test_action_status_controls_keep_start_before_cancel_in_a_wrapping_flex_group():
    template = (
        Path(__file__).parents[1]
        / "templates"
        / "platform"
        / "instagram"
        / "recommendation_action_detail.html"
    ).read_text(encoding="utf-8")
    status_controls = template.split('id="instagram-action-status-title"', 1)[1].split(
        "</section>", 1
    )[0]

    assert 'class="platform-list-actions platform-action-set"' in status_controls
    assert status_controls.index("Iniciar") < status_controls.index("Cancelar")


def test_evaluation_header_keeps_text_and_actions_as_shared_header_siblings():
    root = Path(__file__).parents[1] / "templates" / "platform" / "instagram"
    evaluation_form = (root / "recommendation_action_evaluation_form.html").read_text(encoding="utf-8")
    evaluation_detail = (root / "recommendation_action_evaluation_detail.html").read_text(encoding="utf-8")
    insight_detail = (root / "insight_detail.html").read_text(encoding="utf-8")

    expected_children = [("div", _DETAIL_HEADER_COPY), ("div", _DETAIL_HEADER_ACTIONS)]
    assert _detail_header_children(evaluation_form) == expected_children
    assert _detail_header_children(evaluation_detail) == expected_children
    assert _DETAIL_HEADER_LAYOUT in insight_detail
    for template in (evaluation_form, evaluation_detail):
        assert 'w-100' not in template
        assert 'flex-basis: 100%' not in template

    actions_start = evaluation_form.index(_DETAIL_HEADER_ACTIONS)
    assert actions_start > evaluation_form.index("</p>")
    assert evaluation_form.index("Voltar à ação/teste", actions_start) < evaluation_form.index(
        "tutorial_button(instagram_tutorial", actions_start
    )


def test_evaluation_form_splits_the_dynamic_description_into_two_real_lines():
    template = (
        Path(__file__).parents[1]
        / "templates"
        / "platform"
        / "instagram"
        / "recommendation_action_evaluation_form.html"
    ).read_text(encoding="utf-8")
    header = template.split('aria-labelledby="instagram-action-evaluation-title"', 1)[1].split(
        "</section>", 1
    )[0]

    assert "Ampliar a base de observação para {{ action.title }}." in header
    assert '<span class="d-block">Escolha explicitamente o novo período de dados antes de registrar o aprendizado.</span>' in header
    assert " · escolha explicitamente" not in header
