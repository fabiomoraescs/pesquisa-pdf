"""Coleta paginada e normalização de comentários Meta, sem persistência."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
import unittest

from cryptography.fernet import Fernet

from platform_core.instagram_meta import (
    FacebookLoginProvider,
    InstagramLoginProvider,
    InstagramMetaError,
    InstagramMetaSettings,
    normalize_meta_comment,
)


def _settings() -> InstagramMetaSettings:
    return InstagramMetaSettings(
        facebook_app_id="facebook-app-for-tests",
        facebook_app_secret="facebook-secret-for-tests",
        instagram_app_id="instagram-app-for-tests",
        instagram_app_secret="instagram-secret-for-tests",
        redirect_uri="https://analysis.example/meta/callback",
        api_version="v26.0",
        token_encryption_key=Fernet.generate_key().decode("ascii"),
    )


def _comment(
    external_id: str,
    *,
    text: str | None = "Comentário de teste",
    timestamp: str = "2026-10-14T10:20:30-03:00",
    username: str | None = "leitora_teste",
    like_count: int | None = 0,
    replies_count: int | None = 0,
) -> dict:
    payload = {
        "id": external_id,
        "text": text,
        "timestamp": timestamp,
        "like_count": like_count,
        "replies_count": replies_count,
    }
    if username is not None:
        payload["username"] = username
    return payload


class MetaCommentNormalizationTests(unittest.TestCase):
    def test_normalization_preserves_zero_and_none_and_uses_utc(self):
        item = normalize_meta_comment(
            _comment("comment-1", like_count=0, replies_count=None),
            media_external_id="media-1",
        )

        self.assertEqual(item.external_id, "comment-1")
        self.assertEqual(item.media_external_id, "media-1")
        self.assertIsNone(item.parent_comment_external_id)
        self.assertEqual(item.like_count, 0)
        self.assertIsNone(item.replies_count)
        self.assertEqual(item.published_at, datetime(2026, 10, 14, 13, 20, 30, tzinfo=timezone.utc))

    def test_normalization_accepts_absent_optional_author_and_author_identity_when_present(self):
        no_author = normalize_meta_comment(_comment("comment-1", username=None))
        with_author = normalize_meta_comment(
            {
                **_comment("comment-2", username=None),
                "from": {"id": "author-42", "username": "autor_teste"},
            },
        )

        self.assertIsNone(no_author.author_username)
        self.assertIsNone(no_author.author_external_id)
        self.assertEqual(with_author.author_username, "autor_teste")
        self.assertEqual(with_author.author_external_id, "author-42")

    def test_normalization_preserves_missing_or_null_text_as_unavailable(self):
        missing_text = _comment("comment-missing-text")
        missing_text.pop("text")
        null_text = _comment("comment-null-text", text=None)

        for payload in (missing_text, null_text):
            with self.subTest(payload=payload):
                item = normalize_meta_comment(payload, media_external_id="media-1")
                self.assertIsNone(item.text)
                self.assertEqual(item.external_id, payload["id"])
                self.assertEqual(item.media_external_id, "media-1")
                self.assertEqual(item.like_count, 0)

    def test_reply_without_text_keeps_its_parent_reference(self):
        payload = _comment("reply-without-text")
        payload.pop("text")

        reply = normalize_meta_comment(payload, parent_comment_external_id="root-1")

        self.assertIsNone(reply.text)
        self.assertEqual(reply.parent_comment_external_id, "root-1")

    def test_normalization_rejects_invalid_required_values(self):
        invalid_payloads = (
            {**_comment("comment-1"), "id": ""},
            {**_comment("comment-1"), "text": "   "},
            {**_comment("comment-1"), "timestamp": "not-a-date"},
            {**_comment("comment-1"), "timestamp": "2026-10-14T10:20:30"},
            {**_comment("comment-1"), "text": []},
            {**_comment("comment-1"), "like_count": -1},
            {**_comment("comment-1"), "replies_count": True},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(InstagramMetaError, "formato inesperado") as raised:
                    normalize_meta_comment(payload)
                self.assertEqual(raised.exception.category, "payload_invalid")

    def test_normalization_rejects_self_parent(self):
        with self.assertRaises(InstagramMetaError) as raised:
            normalize_meta_comment(_comment("comment-1"), parent_comment_external_id="comment-1")
        self.assertEqual(raised.exception.category, "payload_invalid")


class MetaCommentProviderTests(unittest.TestCase):
    def _scripted_provider(self, provider_class, responses):
        provider = provider_class(_settings())
        calls = []
        scripted = iter(responses)

        def request_json(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return next(scripted)

        provider._request_json = request_json
        return provider, calls

    def test_facebook_lists_paginated_media_comments_on_graph_host(self):
        provider, calls = self._scripted_provider(FacebookLoginProvider, [
            {
                "data": [_comment("comment-1")],
                "paging": {"cursors": {"after": "cursor-1"}, "next": "opaque-next"},
            },
            {"data": [_comment("comment-2", like_count=None, replies_count=2)]},
        ])

        comments = provider.list_media_comments("media-42", "page-token-very-secret")

        self.assertEqual([item.external_id for item in comments], ["comment-1", "comment-2"])
        self.assertTrue(all(item.media_external_id == "media-42" for item in comments))
        self.assertEqual(urlparse(calls[0][1]).netloc, "graph.facebook.com")
        self.assertIn("/media-42/comments?", calls[0][1])
        self.assertEqual(parse_qs(urlparse(calls[0][1]).query)["fields"], [
            "id,text,timestamp,username,like_count",
        ])
        self.assertEqual(parse_qs(urlparse(calls[1][1]).query)["after"], ["cursor-1"])
        self.assertNotIn("page-token-very-secret", calls[0][1])

    def test_instagram_login_lists_paginated_replies_with_parent_reference(self):
        provider, calls = self._scripted_provider(InstagramLoginProvider, [
            {
                "data": [_comment("reply-1")],
                "paging": {"cursors": {"after": "cursor-r"}, "next": "opaque-next"},
            },
            {"data": [_comment("reply-2", username=None)]},
        ])

        replies = provider.list_comment_replies("comment-root", "instagram-token-very-secret")

        self.assertEqual([item.external_id for item in replies], ["reply-1", "reply-2"])
        self.assertTrue(all(item.parent_comment_external_id == "comment-root" for item in replies))
        self.assertTrue(all(item.media_external_id is None for item in replies))
        self.assertEqual(urlparse(calls[0][1]).netloc, "graph.instagram.com")
        self.assertIn("/comment-root/replies?", calls[0][1])
        self.assertEqual(parse_qs(urlparse(calls[1][1]).query)["after"], ["cursor-r"])
        self.assertEqual(parse_qs(urlparse(calls[0][1]).query)["fields"], [
            "id,text,timestamp,username,like_count",
        ])
        self.assertNotIn("instagram-token-very-secret", calls[0][1])

    def test_both_providers_accept_a_valid_comment_when_meta_omits_text(self):
        facebook_payload = _comment("facebook-without-text")
        facebook_payload.pop("text")
        instagram_payload = _comment("instagram-null-text", text=None)
        facebook, _facebook_calls = self._scripted_provider(FacebookLoginProvider, [{"data": [facebook_payload]}])
        instagram, _instagram_calls = self._scripted_provider(InstagramLoginProvider, [{"data": [instagram_payload]}])

        roots = facebook.list_media_comments("media-1", "page-token")
        replies = instagram.list_comment_replies("root-1", "instagram-token")

        self.assertIsNone(roots.comments[0].text)
        self.assertEqual(roots.comments[0].media_external_id, "media-1")
        self.assertIsNone(replies.comments[0].text)
        self.assertEqual(replies.comments[0].parent_comment_external_id, "root-1")

    def test_repeated_cursor_is_rejected_instead_of_returning_partial_comments(self):
        provider, _calls = self._scripted_provider(FacebookLoginProvider, [
            {
                "data": [_comment("comment-1")],
                "paging": {"cursors": {"after": "cursor-1"}, "next": "opaque-next-1"},
            },
            {
                "data": [_comment("comment-2")],
                "paging": {"cursors": {"after": "cursor-1"}, "next": "opaque-next-2"},
            },
        ])

        with self.assertRaises(InstagramMetaError) as raised:
            provider.list_media_comments("media-42", "token")
        self.assertEqual(raised.exception.category, "payload_invalid")

    def test_page_limit_is_rejected_instead_of_returning_partial_replies(self):
        provider, _calls = self._scripted_provider(InstagramLoginProvider, [
            {
                "data": [_comment("reply-1")],
                "paging": {"cursors": {"after": "cursor-1"}, "next": "opaque-next"},
            },
        ])

        with patch("platform_core.instagram_meta.SYNC_COMMENT_MAX_PAGES", 1):
            with self.assertRaises(InstagramMetaError) as raised:
                provider.list_comment_replies("comment-root", "token")
        self.assertEqual(raised.exception.category, "pagination_limit")

    def test_record_limit_is_rejected_instead_of_returning_partial_comments(self):
        provider, _calls = self._scripted_provider(FacebookLoginProvider, [
            {"data": [_comment("comment-1"), _comment("comment-2")]},
        ])

        with patch("platform_core.instagram_meta.SYNC_COMMENT_MAX_RECORDS_PER_EDGE", 1):
            with self.assertRaises(InstagramMetaError) as raised:
                provider.list_media_comments("media-42", "token")
        self.assertEqual(raised.exception.category, "pagination_limit")

    def test_empty_data_is_a_valid_terminal_page(self):
        provider, _calls = self._scripted_provider(FacebookLoginProvider, [{"data": []}])

        comments = provider.list_media_comments("media-42", "token")

        self.assertEqual(comments.comments, ())
        self.assertEqual(comments.pages_queried, 1)

    def test_missing_or_malformed_data_is_rejected_instead_of_becoming_an_empty_page(self):
        malformed_payloads = (
            {"paging": {"cursors": {"before": "cursor"}}},
            {"data": None},
            {"data": {}},
            {"data": ["not-a-comment"]},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                provider, _calls = self._scripted_provider(InstagramLoginProvider, [payload])
                with self.assertRaises(InstagramMetaError) as raised:
                    provider.list_comment_replies("comment-root", "token")
                self.assertEqual(raised.exception.category, "payload_invalid")

    def test_malformed_paging_and_cursors_are_rejected(self):
        malformed_payloads = (
            {"data": [], "paging": []},
            {"data": [], "paging": {}},
            {"data": [], "paging": {"next": "opaque"}},
            {"data": [], "paging": {"cursors": []}},
            {"data": [], "paging": {"cursors": {}}},
            {"data": [], "paging": {"cursors": {"after": 1}}},
            {"data": [], "paging": {"cursors": {"after": ""}}},
            {"data": [], "paging": {"cursors": {"before": 1}}},
            {"data": [], "paging": {"cursors": {"before": "before"}, "next": ""}},
            {"data": [], "paging": {"cursors": {"before": "before"}, "next": 1}},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                provider, _calls = self._scripted_provider(FacebookLoginProvider, [payload])
                with self.assertRaises(InstagramMetaError) as raised:
                    provider.list_media_comments("media-42", "token")
                self.assertEqual(raised.exception.category, "payload_invalid")

    def test_terminal_cursor_without_after_is_not_treated_as_a_failure(self):
        provider, _calls = self._scripted_provider(InstagramLoginProvider, [
            {"data": [], "paging": {"cursors": {"before": "opaque-before"}}},
        ])

        replies = provider.list_comment_replies("comment-root", "token")

        self.assertEqual(replies.comments, ())
        self.assertEqual(replies.pages_queried, 1)

    def test_provider_errors_propagate_without_reclassification_or_partial_result(self):
        provider = FacebookLoginProvider(_settings())
        for category in ("reconnect_required", "permission_denied", "rate_limited"):
            with self.subTest(category=category):
                provider._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    InstagramMetaError(category, "safe error")
                )
                with self.assertRaises(InstagramMetaError) as raised:
                    provider.list_media_comments("media-42", "token")
                self.assertEqual(raised.exception.category, category)

    def test_network_error_is_normalized_as_temporary(self):
        provider = FacebookLoginProvider(_settings())
        with patch("platform_core.instagram_meta.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(InstagramMetaError) as raised:
                provider.list_media_comments("media-42", "token")
        self.assertEqual(raised.exception.category, "temporary")


class MetaCommentCollectionNoPersistenceTests(unittest.TestCase):
    def test_provider_collection_does_not_write_analytics_or_comment_models(self):
        provider = FacebookLoginProvider(_settings())
        provider._request_json = lambda _method, url, **_kwargs: {
            "data": [_comment("reply-1" if "/replies?" in url else "comment-1")]
        }
        # A camada autorizada nesta rodada não pode sequer tocar na sessão;
        # persistência fica para o motor transacional da rodada seguinte.
        with patch("platform_core.instagram_meta.db") as database:
            provider.list_media_comments("media-42", "page-token-very-secret")
            provider.list_comment_replies("comment-1", "page-token-very-secret")
        database.session.assert_not_called()
