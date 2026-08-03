"""Focused regression coverage for Anime discovery frontend wiring."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support direct execution: python tests/test_anime_frontend.py
    from _test_environment import TEST_ROOT

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
        cls.app = _read("frontend", "src", "app.ts")
        cls.pages = _read("backend", "routes", "pages.py")

    def test_search_offers_anime_and_opens_slug_detail(self):
        self.assertIn('<option value="anime">Anime</option>', self.frontend)
        self.assertIn('anime: { placeholder: "Search anime titles…"', self.discovery)
        self.assertIn('showDetail("anime", String(result.slug || result.id))', self.discovery)
        self.assertIn('`/api/${kind}/${encodeURIComponent(id)}`', self.discovery)

    def test_anime_search_results_are_stably_prioritized_by_format(self):
        self.assertIn("function animeSearchResultsByFormat", self.discovery)
        self.assertIn('normalized === "tv"', self.discovery)
        self.assertIn('normalized === "movie"', self.discovery)
        self.assertIn('normalized === "ova"', self.discovery)
        self.assertIn('normalized === "ona"', self.discovery)
        self.assertIn('normalized === "special"', self.discovery)
        self.assertIn("first.providerIndex - second.providerIndex", self.discovery)
        self.assertIn('type === "anime"\n        ? animeSearchResultsByFormat(data.results)', self.discovery)

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
        self.assertIn('`/icons/${provider}.svg`', self.discovery)
        self.assertIn('`Open on ${label}`', self.discovery)
        self.assertIn('anime-resource-${provider}', self.discovery)
        self.assertIn('.anime-resource-links .anime-resource-icon', self.stylesheet)
        self.assertIn('.anime-resource-animethemes img { filter: invert(1); }', self.stylesheet)

    def test_anime_detail_omits_synopsis_but_keeps_metadata_and_links(self):
        self.assertIn('`AnimeThemes ID: ${data.id}`', self.discovery)
        self.assertIn("appendAnimeSeriesLinks(meta, data.series)", self.discovery)
        self.assertIn("appendAnimeExternalLinks(meta, animeExternalLinks(data))", self.discovery)
        self.assertNotIn("data.synopsis", self.discovery)
        self.assertNotIn(".anime-synopsis", self.stylesheet)

    def test_mapping_management_separates_admin_actions_and_user_proposals(self):
        self.assertIn('"Manage mappings"', self.discovery)
        self.assertIn('currentUser?.role === "admin"', self.discovery)
        self.assertIn('if (!theme.id || (isAdmin && !manageMappings)) return null', self.discovery)
        self.assertIn('&& manageMappings', self.discovery)
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
        self.assertIn('"Suggest a mapping"', self.discovery)
        self.assertIn('"Mapping suggestion pending"', self.discovery)
        self.assertIn('`${themeEndpoint}/mapping-proposals`', self.discovery)
        self.assertIn('`${proposalEndpoint}/approve`', self.discovery)
        self.assertIn('method: "DELETE"', self.discovery)
        self.assertIn('theme.proposals || mapping?.proposals', self.discovery)
        self.assertIn('automaticMatchMethod === "recording-search"', self.discovery)
        self.assertIn('automaticMatchMethod === "artist-discography-title"', self.discovery)
        self.assertIn('"Manual · confirmed"', self.discovery)
        self.assertIn(".anime-mapping-editor", self.stylesheet)
        self.assertIn(".anime-mapping-confirmation", self.stylesheet)
        self.assertIn(".anime-mapping-provenance", self.stylesheet)

    def test_mapping_editor_links_to_a_prefilled_musicbrainz_search(self):
        self.assertIn('new URL("https://musicbrainz.org/search")', self.discovery)
        self.assertIn('url.searchParams.set("query", search)', self.discovery)
        self.assertIn('url.searchParams.set("type", "release_group")', self.discovery)
        self.assertIn('/^(unknown|unknown artist)$/i.test(artist.trim())', self.discovery)
        self.assertIn('search.target = "_blank"', self.discovery)
        self.assertIn('search.rel = "noreferrer"', self.discovery)
        self.assertIn('"Search MusicBrainz"', self.discovery)

    def test_theme_detail_groups_metadata_and_mapping_states(self):
        self.assertIn('opening: "Openings", ending: "Endings"', self.discovery)
        self.assertIn("animeEpisodes(theme.episodes)", self.discovery)
        self.assertIn('mapping?.state || mapping?.status', self.discovery)
        for state in ("resolved", "ambiguous", "unmatched", "failed", "pending"):
            self.assertIn(f'"{state}"', self.discovery)
        self.assertIn("mapping?.releaseGroups || mapping?.candidates", self.discovery)
        self.assertIn('animeContext: animeRequestContext(theme)', self.discovery)
        self.assertIn('showDetail("release-group", id)', self.discovery)

    def test_theme_sections_reuse_collapsible_discography_navigation(self):
        self.assertIn('layout.className = "discography-layout anime-theme-layout"', self.discovery)
        self.assertIn('index.className = "discography-nav anime-theme-nav"', self.discovery)
        self.assertIn('section.className = "discography-section anime-theme-section"', self.discovery)
        self.assertIn('section.open = ui!.openSections.has(kind)', self.discovery)
        self.assertIn('ui.openSections.add(defaultSection)', self.discovery)
        self.assertIn('section.replaceChildren(summary)', self.discovery)
        self.assertIn('section.scrollIntoView({ behavior: "smooth", block: "start" })', self.discovery)

    def test_resolution_is_progressive_polled_and_merged_by_theme_id(self):
        self.assertIn(
            'postJson(`/api/anime/${encodeURIComponent(watcher.slug)}/resolve`, {',
            self.discovery,
        )
        self.assertIn('themeIds: [themeId]', self.discovery)
        self.assertIn('requestedThemeIds: Set<string>', self.discovery)
        self.assertIn('watcher.requestedThemeIds.has(themeId)', self.discovery)
        self.assertIn('new IntersectionObserver((entries)', self.discovery)
        self.assertIn('{ rootMargin: "350px 0px" }', self.discovery)
        self.assertIn(
            '`/api/anime/${encodeURIComponent(watcher.slug)}/resolution`',
            self.discovery,
        )
        self.assertIn("const mappings = payload.mappings || {}", self.discovery)
        self.assertIn("mappings[String(theme.id)]", self.discovery)
        self.assertIn("payload.polling !== undefined", self.discovery)
        self.assertIn("return payload.polling === true", self.discovery)
        self.assertIn('"pending", "queued", "running", "resolving"', self.discovery)

    def test_candidates_are_on_demand_limited_and_show_plex_links(self):
        self.assertIn('candidateDetails.className = "anime-mapping-candidates"', self.discovery)
        self.assertIn("candidateDetails.open = true", self.discovery)
        self.assertIn('groups.slice(0, 3)', self.discovery)
        self.assertIn('showAll ? "Show fewer" : `Show all ${groups.length}`', self.discovery)
        self.assertIn('window.matchMedia("(prefers-reduced-motion: reduce)").matches', self.discovery)
        self.assertIn('scrollTarget.scrollIntoView({', self.discovery)
        self.assertIn('candidate.availableInPlex', self.discovery)
        self.assertIn('"/icons/plex.svg"', self.discovery)
        self.assertIn('"service-icon-link anime-candidate-plex"', self.discovery)

    def test_admin_can_confirm_an_ambiguous_candidate_in_management_mode(self):
        self.assertIn('const canConfirmAmbiguousCandidate = currentUser?.role === "admin"', self.discovery)
        self.assertIn('&& state === "ambiguous"', self.discovery)
        self.assertIn('&& groups.length > 0', self.discovery)
        self.assertNotIn('supportedAmbiguousConfirmation', self.discovery)
        self.assertIn('&& !mapping?.mappingSource', self.discovery)
        self.assertIn('confirm.className = "anime-candidate-confirm"', self.discovery)
        self.assertIn('confirm.textContent = "Confirm match"', self.discovery)
        self.assertIn('confirmAutomatic: true', self.discovery)
        self.assertIn('releaseGroup: releaseGroupId', self.discovery)
        self.assertIn('.anime-candidate-confirm { grid-area: confirmation;', self.stylesheet)

    def test_release_group_detail_links_anime_themes_and_preserves_request_context(self):
        self.assertIn('heading.textContent = "Featured in anime"', self.discovery)
        self.assertIn('section.className = "release-anime-themes"', self.discovery)
        self.assertIn('`${base}#theme-${encodeURIComponent(themeId)}`', self.discovery)
        self.assertIn('association.songTitle', self.discovery)
        self.assertIn('if (animeThemes.length) results.append(createReleaseAnimeThemes(animeThemes))', self.discovery)
        self.assertIn('animeContext: releaseAnimeRequestContext(data)', self.discovery)
        self.assertIn('second.specificity - first.specificity', self.discovery)
        self.assertIn('detailRequests.delete(`release-group:${String(id)}`)', self.discovery)
        self.assertIn(".release-anime-theme-link", self.stylesheet)

    def test_anime_request_context_is_sent_and_history_links_back_to_theme(self):
        for field in (
            "animeSlug", "animeName", "themeId", "themeLabel", "songId", "songTitle"
        ):
            self.assertIn(f"{field}:", self.discovery)
        self.assertIn('...(releaseGroup.animeContext || {})', self.discovery)
        self.assertIn('link.className = "history-anime-context"', self.app)
        self.assertIn('`#theme-${encodeURIComponent(context.themeId)}`', self.app)
        self.assertIn('card.id = `theme-${theme.id}`', self.discovery)
        self.assertIn('window.location.hash.match(/^#theme-(.+)$/)', self.discovery)
        self.assertIn('return decodeURIComponent(encoded)', self.discovery)
        self.assertIn('const hashSection = hashThemeId', self.discovery)
        self.assertIn('ui.openSections.add(hashSection)', self.discovery)
        self.assertIn('target?.scrollIntoView({ block: "start" })', self.discovery)

    def test_anime_theme_cards_have_mobile_layout(self):
        self.assertIn(".anime-theme-card", self.stylesheet)
        self.assertIn(".anime-theme-mapping", self.stylesheet)
        self.assertIn(".anime-release-candidates", self.stylesheet)
        self.assertIn(".anime-series-grid", self.stylesheet)
        self.assertIn(".anime-series-card", self.stylesheet)
        self.assertIn(".anime-theme-card { grid-template-columns: 1fr;", self.stylesheet)
        self.assertIn('"content content content"', self.stylesheet)
        self.assertIn('"recommendation action plex"', self.stylesheet)
        self.assertIn(".detail-cover.anime-cover { width: min(116px, 34vw);", self.stylesheet)
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
