"""Classificação automática local de comentários: contrato, histórico e rotas."""

from __future__ import annotations

import csv
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import func, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_comment_classification import (
    CommentClassificationError,
    CommentClassificationPersistenceError,
    CommentClassificationResult,
    CommentClassifierInput,
    LOCAL_COMMENT_CLASSIFIER_KEY,
    LOCAL_COMMENT_CLASSIFIER_VERSION,
    LocalRuleBasedCommentClassifier,
    classify_project_comments,
    plan_comment_classification,
)
from platform_core.instagram_comment_import import COMMENT_CANONICAL_HEADERS, parse_instagram_comment_import
from platform_core.instagram_comment_storage import persist_instagram_comment_import
from platform_core.instagram_comments import classify_comment, effective_assessment
from platform_core.models import (
    AnalyticsProject,
    InstagramAccount,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramMedia,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


def _csv_bytes(*rows: dict[str, str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COMMENT_CANONICAL_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _row(comment_external_id: str = "comment-1", text: str = "Como faço a inscrição?") -> dict[str, str]:
    return {
        "media_external_id": "post-1",
        "comment_external_id": comment_external_id,
        "comment_text": text,
        "published_at": "2026-10-02T14:30:00-03:00",
        "observed_at": "2026-10-03T09:00:00-03:00",
        "parent_comment_external_id": "",
        "author_username": "leitora",
        "author_external_id": "user-1",
        "like_count": "3",
        "replies_count": "0",
    }


class InstagramCommentClassificationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Recepção automática")
        self.account = InstagramAccount(
            analytics_project=self.project, username="revista", username_normalized="revista",
        )
        self.media = InstagramMedia(
            instagram_account=self.account, external_id="post-1", caption_original="Publicação",
            published_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, name="Projeto privado")
        db.session.add_all((self.project, self.account, self.media, self.other_project))
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def import_comments(self, *rows: dict[str, str]) -> None:
        dataset = parse_instagram_comment_import("comentarios.csv", _csv_bytes(*rows))
        persist_instagram_comment_import(self.project, dataset)

    def assessments(self) -> list[InstagramCommentAssessment]:
        return db.session.scalars(
            select(InstagramCommentAssessment).order_by(InstagramCommentAssessment.created_at, InstagramCommentAssessment.id)
        ).all()

    def test_local_classifier_contract_is_deterministic_and_transparent(self):
        classifier = LocalRuleBasedCommentClassifier()
        question = classifier.classify(CommentClassifierInput("c1", "v1", "Como faço a inscrição?"))
        self.assertEqual(question.classifier_key, LOCAL_COMMENT_CLASSIFIER_KEY)
        self.assertEqual(question.classifier_version, LOCAL_COMMENT_CLASSIFIER_VERSION)
        self.assertEqual(question.intent, "question")
        self.assertTrue(question.needs_response)
        self.assertIn("Pergunta detectada", question.rationale)
        self.assertEqual(classifier.classify(CommentClassifierInput("c2", "v2", "Parabéns, adorei! ")).sentiment, "positive")
        self.assertEqual(classifier.classify(CommentClassifierInput("c3", "v3", "Péssimo atendimento.")).sentiment, "negative")
        self.assertEqual(classifier.classify(CommentClassifierInput("c4", "v4", "Obrigado pela explicação.")).topic_normalized, "agradecimento")
        self.assertEqual(classifier.classify(CommentClassifierInput("c5", "v5", "Registro neutro.")).sentiment, "neutral")
        skipped = classifier.classify(CommentClassifierInput("c6", "v6", "   "))
        self.assertIn("ignorado", skipped.rationale.lower())
        self.assertIn("URL", classifier.classify(CommentClassifierInput("c7", "v7", "https://example.org")).rationale)
        self.assertIn("símbolos", classifier.classify(CommentClassifierInput("c8", "v8", "🔥")).rationale)

    def test_batch_persists_automatic_assessment_and_is_idempotent(self):
        self.import_comments(_row())
        preview = plan_comment_classification(self.project)
        self.assertEqual((preview.eligible, preview.classified, preview.already_classified), (1, 1, 0))
        result = classify_project_comments(self.project)
        self.assertEqual((result.classified, result.already_classified, result.errors), (1, 0, 0))
        assessment = self.assessments()[0]
        self.assertEqual(assessment.source, "automatic")
        self.assertEqual(assessment.classifier_key, LOCAL_COMMENT_CLASSIFIER_KEY)
        self.assertEqual(assessment.classifier_version, LOCAL_COMMENT_CLASSIFIER_VERSION)
        self.assertEqual(assessment.intent, "question")
        self.assertEqual(assessment.instagram_comment_text_version_id, assessment.text_version.id)
        self.assertIsNotNone(assessment.confidence)
        self.assertTrue(assessment.rationale)
        repeated = classify_project_comments(self.project)
        self.assertEqual((repeated.classified, repeated.already_classified), (0, 1))
        self.assertEqual(len(self.assessments()), 1)

    def test_batch_skips_comments_without_any_text_version(self):
        comment = InstagramComment(
            instagram_media=self.media,
            external_id="comment-without-text-version",
            published_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
            original_text=None,
        )
        db.session.add(comment)
        db.session.commit()

        preview = plan_comment_classification(self.project)
        self.assertEqual(
            (preview.total_comments, preview.eligible, preview.classified, preview.skipped, preview.errors),
            (1, 0, 0, 1, 0),
        )
        completed = classify_project_comments(self.project)
        self.assertEqual(
            (completed.total_comments, completed.eligible, completed.classified, completed.skipped, completed.errors),
            (1, 0, 0, 1, 0),
        )
        self.assertEqual(len(self.assessments()), 0)

    def test_new_classifier_or_text_version_creates_new_automatic_history(self):
        self.import_comments(_row(text="Obrigado pela ajuda."))
        classify_project_comments(self.project)
        new_classifier = LocalRuleBasedCommentClassifier(classifier_version="comment-local-rules-v2")
        result = classify_project_comments(self.project, new_classifier)
        self.assertEqual(result.classified, 1)
        self.assertEqual(len(self.assessments()), 2)
        self.assertTrue(any(
            assessment.classifier_version == "comment-local-rules-v2" for assessment in self.assessments()
        ))
        changed = _row(text="Como acompanho a inscrição?")
        changed["observed_at"] = "2026-10-04T09:00:00-03:00"
        self.import_comments(changed)
        result = classify_project_comments(self.project)
        self.assertEqual(result.classified, 1)
        self.assertEqual(len(self.assessments()), 3)
        self.assertEqual(len({assessment.instagram_comment_text_version_id for assessment in self.assessments()}), 2)

    def test_human_review_is_effective_after_explicit_automatic_reprocess(self):
        self.import_comments(_row())
        classify_project_comments(self.project)
        comment = db.session.scalar(select(InstagramComment))
        classify_comment(comment, self.user.id, {
            "sentiment": "mixed", "intensity": "medium", "intent": "information", "topic": "Inscrição",
            "topic_normalized": "inscricao", "needs_response": "no", "risk": "none", "rationale": "Revisão humana.",
        })
        db.session.commit()
        reprocessed = classify_project_comments(self.project, reprocess=True)
        self.assertEqual(reprocessed.classified, 1)
        history = self.assessments()
        self.assertEqual(len(history), 3)
        effective = effective_assessment(history)
        self.assertEqual(effective.source, "human_review")
        self.assertEqual(effective.rationale, "Revisão humana.")
        self.assertTrue(any(item.source == "automatic" for item in history))

    def test_service_rolls_back_unexpected_persistence_failure(self):
        self.import_comments(_row("comment-1"), _row("comment-2", "Parabéns pelo conteúdo."))
        with patch("platform_core.instagram_comment_classification.db.session.commit", side_effect=RuntimeError("controlled")):
            with self.assertRaises(CommentClassificationPersistenceError):
                classify_project_comments(self.project)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramCommentAssessment)), 0)

    def test_invalid_entry_is_reported_without_aborting_other_comments(self):
        self.import_comments(_row("invalid", "Entrada inválida."), _row("valid", "Obrigado pelo retorno."))

        class MixedClassifier(LocalRuleBasedCommentClassifier):
            def classify(self, item):
                if "inválida" in item.text:
                    return CommentClassificationResult(
                        classifier_key=self.classifier_key,
                        classifier_version=self.classifier_version,
                        sentiment="not-a-domain-value",
                        rationale="Resultado intencionalmente inválido para o teste.",
                    )
                return super().classify(item)

        summary = classify_project_comments(self.project, MixedClassifier())
        self.assertEqual((summary.classified, summary.errors), (1, 1))
        self.assertEqual(len(self.assessments()), 1)
        self.assertIn("inválido", summary.error_messages[0])

    def test_routes_enforce_csrf_ownership_and_archived_read_only(self):
        self.import_comments(_row())
        preview_url = f"/analytics/instagram/projects/{self.project.id}/comments/classify"
        comments_page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/comments")
        self.assertIn("Classificar comentários", comments_page.get_data(as_text=True))
        preview = self.client.get(preview_url)
        self.assertEqual(preview.status_code, 200)
        preview_html = preview.get_data(as_text=True)
        self.assertIn("Classificador local baseado em regras", preview_html)
        self.assertIn(LOCAL_COMMENT_CLASSIFIER_VERSION, preview_html)
        self.assertIn("humanas efetivas", preview_html)
        self.assertEqual(self.client.post(preview_url, data={}).status_code, 400)
        completed = self.client.post(preview_url, data={"csrf_token": csrf_from(preview)}, follow_redirects=True)
        self.assertEqual(completed.status_code, 200)
        self.assertIn("Classificação automática concluída", completed.get_data(as_text=True))
        repeated_preview = self.client.get(preview_url)
        repeated = self.client.post(
            preview_url, data={"csrf_token": csrf_from(repeated_preview)}, follow_redirects=True,
        )
        self.assertIn("Nenhum comentário novo para classificar", repeated.get_data(as_text=True))
        comment = db.session.scalar(select(InstagramComment))
        detail = self.client.get(f"/analytics/instagram/projects/{self.project.id}/comments/{comment.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertIn("Avaliação efetiva", detail_html)
        self.assertIn("Classificação automática", detail_html)
        self.assertIn("Versão do classificador", detail_html)
        self.assertIn("Justificativa", detail_html)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(preview_url).status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        with self.assertRaises(CommentClassificationError):
            classify_project_comments(self.project)
        self.assertEqual(self.client.get(preview_url).status_code, 404)

    def test_empty_project_shows_a_safe_classification_preview(self):
        response = self.client.get(f"/analytics/instagram/projects/{self.project.id}/comments/classify")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nenhum comentário disponível para classificação", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
