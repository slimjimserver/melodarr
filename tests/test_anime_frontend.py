"""Focused regression coverage for Anime discovery frontend wiring."""

import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as file:
        return file.read()


class AnimeFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frontend = _read("frontend", "static", "index.html")
        cls.discovery = _read("frontend", "src", "discovery.ts")
        cls.stylesheet = _read("frontend", "src", "style.css")
        cls.pages = _read("backend", "routes", "pages.py")

    def test_search_offers_anime_and_opens_slug_detail(self):
        self.assertIn('<option value="anime">Anime</option>', self.frontend)
        self.assertIn('anime: { placeholder: "Search anime titles…"', self.discovery)
        self.assertIn('showDetail("anime", String(result.slug || result.id))', self.discovery)
        self.assertIn('`/api/${kind}/${encodeURIComponent(id)}`', self.discovery)

    def test_anime_deep_link_is_served_and_restored_by_spa(self):
        self.assertIn(
            'blueprint.add_url_rule("/anime/<slug>", view_func=frontend_index)',
            self.pages,
        )
        self.assertIn("artists|albums|releases|anime|series", self.discovery)
        self.assertIn('anime: "anime"', self.discovery)
        self.assertIn('"release", "anime"', self.discovery)

    def test_series_links_open_a_related_anime_catalog(self):
        self.assertIn(
            'blueprint.add_url_rule("/series/<slug>", view_func=frontend_index)',
            self.pages,
        )
        self.assertIn('link.href = detailPath("series", slug)', self.discovery)
        self.assertIn('showDetail("series", slug)', self.discovery)
        self.assertIn('`/api/${kind}/${encodeURIComponent(id)}`', self.discovery)
        self.assertIn('renderAnimeSeriesDetail(data)', self.discovery)
        self.assertIn('showDetail("anime", animeSlug)', self.discovery)
        self.assertIn('`${themeCount} ${themeCount === 1 ? "theme" : "themes"}`', self.discovery)

    def test_anime_external_links_are_limited_and_credit_animethemes(self):
        self.assertIn('=== "myanimelist"', self.discovery)
        self.assertIn('label: "MyAnimeList"', self.discovery)
        self.assertIn('label: "AnimeThemes"', self.discovery)
        self.assertIn('https://animethemes.moe/anime/', self.discovery)
        self.assertIn('https://animethemes.moe/series/', self.discovery)

    def test_anime_detail_omits_synopsis_but_keeps_metadata_and_links(self):
        self.assertIn('`AnimeThemes ID: ${data.id}`', self.discovery)
        self.assertIn("appendAnimeSeriesLinks(meta, data.series)", self.discovery)
        self.assertIn("appendAnimeExternalLinks(meta, animeExternalLinks(data))", self.discovery)
        self.assertNotIn("data.synopsis", self.discovery)
        self.assertNotIn(".anime-synopsis", self.stylesheet)

    def test_admin_can_correct_and_unlink_manual_song_mappings(self):
        self.assertIn('currentUser?.role !== "admin"', self.discovery)
        self.assertIn('"Correct mapping"', self.discovery)
        self.assertIn('"Override mapping"', self.discovery)
        self.assertIn('placeholder = "Release-group URL or MBID"', self.discovery)
        self.assertIn('method: "PUT"', self.discovery)
        self.assertIn('method: "DELETE"', self.discovery)
        self.assertIn('mapping?.mappingSource === "local"', self.discovery)
        self.assertIn('mapping?.mappingSource === "seed"', self.discovery)
        self.assertIn('seedMapping ? "Suppress" : "Unlink"', self.discovery)
        self.assertIn('"Confirm recommended match"', self.discovery)
        self.assertIn("confirmAutomatic: true", self.discovery)
        self.assertIn('automaticMatchMethod === "recording-search"', self.discovery)
        self.assertIn('automaticMatchMethod === "artist-discography-title"', self.discovery)
        self.assertIn('"Manual · confirmed"', self.discovery)
        self.assertIn(".anime-mapping-editor", self.stylesheet)
        self.assertIn(".anime-mapping-confirmation", self.stylesheet)
        self.assertIn(".anime-mapping-provenance", self.stylesheet)

    def test_theme_detail_groups_metadata_and_mapping_states(self):
        self.assertIn('opening: "Openings", ending: "Endings"', self.discovery)
        self.assertIn("animeEpisodes(theme.episodes)", self.discovery)
        self.assertIn('mapping?.state || mapping?.status', self.discovery)
        for state in ("resolved", "ambiguous", "unmatched", "failed", "pending"):
            self.assertIn(f'"{state}"', self.discovery)
        self.assertIn("mapping?.releaseGroups || mapping?.candidates", self.discovery)
        self.assertIn('requestReleaseGroup({ id, button: requestButton })', self.discovery)
        self.assertIn('showDetail("release-group", id)', self.discovery)

    def test_resolution_is_requested_polled_and_merged_by_theme_id(self):
        self.assertIn(
            'postJson(`/api/anime/${encodeURIComponent(slug)}/resolve`, {})',
            self.discovery,
        )
        self.assertIn(
            '`/api/anime/${encodeURIComponent(watcher.slug)}/resolution`',
            self.discovery,
        )
        self.assertIn("const mappings = payload.mappings || {}", self.discovery)
        self.assertIn("mappings[String(theme.id)]", self.discovery)
        self.assertIn("payload.polling === true", self.discovery)
        self.assertIn('"pending", "queued", "running", "resolving"', self.discovery)

    def test_anime_theme_cards_have_mobile_layout(self):
        self.assertIn(".anime-theme-card", self.stylesheet)
        self.assertIn(".anime-theme-mapping", self.stylesheet)
        self.assertIn(".anime-release-candidates", self.stylesheet)
        self.assertIn(".anime-series-grid", self.stylesheet)
        self.assertIn(".anime-series-card", self.stylesheet)
        self.assertIn(".anime-theme-card { grid-template-columns: 1fr;", self.stylesheet)
        self.assertIn('"content content"', self.stylesheet)
        self.assertIn('"recommendation action"', self.stylesheet)
        self.assertIn(
            ".anime-release-candidate > .card-open { grid-area: content;",
            self.stylesheet,
        )
        self.assertIn(
            ".anime-release-candidate.artist-card > .release-group-request",
            self.stylesheet,
        )


if __name__ == "__main__":
    unittest.main()
