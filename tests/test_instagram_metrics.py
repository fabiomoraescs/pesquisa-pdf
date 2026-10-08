"""Contrato matemático das métricas derivadas do Instagram."""

from __future__ import annotations

import unittest

from platform_core.instagram_metrics import (
    DERIVED_METRICS_BY_ID,
    DERIVED_METRICS_VERSION,
    ContentTypeInput,
    calculate_derived_metrics,
    compare_content_types,
    follower_variation,
    normalized_content_type,
    ratio_percentage,
    summarize_values,
)


class InstagramMetricsTests(unittest.TestCase):
    def test_definitions_are_versioned_and_traceable(self):
        definition = DERIVED_METRICS_BY_ID["interaction_rate_by_reach"]
        self.assertEqual(definition.version, DERIVED_METRICS_VERSION)
        self.assertEqual(definition.formula, "interactions / reach * 100")
        self.assertEqual(definition.fields, ("interactions", "reach"))
        self.assertEqual(definition.unit, "%")
        self.assertIn("NULL", definition.null_rule)

    def test_interaction_rate_null_zero_and_above_one_hundred_percent(self):
        self.assertEqual(ratio_percentage(180, 1500), 12.0)
        self.assertIsNone(ratio_percentage(None, 1500))
        self.assertIsNone(ratio_percentage(100, None))
        self.assertIsNone(ratio_percentage(100, 0))
        self.assertEqual(ratio_percentage(0, 1000), 0.0)
        self.assertEqual(ratio_percentage(150, 100), 150.0)

    def test_all_rates_keep_interactions_canonical(self):
        rates = calculate_derived_metrics({
            "reach": 1000, "interactions": None, "likes": 40, "comments_count": 3,
            "shares": 2, "saves": 5, "follows_generated": 1, "impressions": 1200,
        })
        self.assertIsNone(rates["interaction_rate_by_reach"])
        self.assertIsNone(rates["interaction_rate_by_impressions"])
        self.assertEqual(rates["likes_rate_by_reach"], 4.0)
        self.assertEqual(rates["comments_rate_by_reach"], 0.3)
        self.assertEqual(rates["shares_rate_by_reach"], 0.2)
        self.assertEqual(rates["saves_rate_by_reach"], 0.5)
        self.assertEqual(rates["follows_generated_rate_by_reach"], 0.1)

    def test_mean_median_coverage_and_zero_are_distinct_from_null(self):
        stats = summarize_values("rate", "Taxa", (5, None, 10), 3)
        self.assertEqual(stats.known, 2)
        self.assertEqual(stats.publications, 3)
        self.assertTrue(stats.partial)
        self.assertEqual(stats.mean_value, 7.5)
        self.assertEqual(stats.median_value, 7.5)
        asymmetrical = summarize_values("rate", "Taxa", (5, 10, 100), 3)
        self.assertAlmostEqual(asymmetrical.mean_value, 38.333333333333336)
        self.assertEqual(asymmetrical.median_value, 10)
        zero = summarize_values("reach", "Alcance", (1000, 0, 500), 3)
        self.assertEqual((zero.known, zero.mean_value, zero.median_value), (3, 500, 500))

    def test_follower_variation_uses_last_two_valid_stocks(self):
        variation = follower_variation((8450, 8675))
        self.assertEqual(variation.absolute, 225)
        self.assertAlmostEqual(variation.percentage, 2.662721893491124)
        from_zero = follower_variation((0, 100))
        self.assertEqual(from_zero.absolute, 100)
        self.assertIsNone(from_zero.percentage)
        self.assertIsNone(follower_variation((8450,)).absolute)

    def test_content_type_comparison_uses_rates_per_publication(self):
        def item(media_id, media_type, reach, interactions):
            raw = {"reach": reach, "interactions": interactions}
            return ContentTypeInput(media_id, media_type, raw, calculate_derived_metrics(raw))

        comparisons = {comparison.key: comparison for comparison in compare_content_types((
            item("reel-1", "REEL", 1000, 100),
            item("reel-2", "reels", 0, 0),
            item("carousel-1", "CAROUSEL_ALBUM", 400, 40),
            item("carousel-2", "carrossel", None, 50),
            item("image-1", "IMAGE", 500, 25),
            item("image-2", "image", 1000, None),
        ))}
        reels = comparisons["reel"]
        self.assertEqual(reels.publications, 2)
        self.assertEqual((reels.reach.known, reels.reach.median_value), (2, 500))
        self.assertEqual((reels.interaction_rate_by_reach.known, reels.interaction_rate_by_reach.median_value), (1, 10.0))
        carousels = comparisons["carousel"]
        self.assertEqual((carousels.publications, carousels.reach.known, carousels.reach.median_value), (2, 1, 400))
        self.assertEqual(carousels.interaction_rate_by_reach.median_value, 10.0)
        images = comparisons["image"]
        self.assertEqual((images.interaction_rate_by_reach.known, images.interaction_rate_by_reach.median_value), (1, 5.0))

    def test_content_type_labels_do_not_rewrite_unknown_raw_values(self):
        self.assertEqual(normalized_content_type("REEL"), ("reel", "Reels"))
        self.assertEqual(normalized_content_type("CAROUSEL_ALBUM"), ("carousel", "Carrosséis"))
        self.assertEqual(normalized_content_type("IMAGE"), ("image", "Imagens"))
        self.assertEqual(normalized_content_type("STORY"), ("other", "Outro"))
        self.assertEqual(normalized_content_type(None), ("unknown", "Não informado"))


if __name__ == "__main__":
    unittest.main()
