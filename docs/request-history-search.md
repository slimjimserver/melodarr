# Request-history search aliases

Search reads only the authorized account's request rows and local catalog data.
Request-supplied anime names remain on their request rows and are searchable
there. A submitted anime slug can reference existing catalog aliases, but does
not establish that the accompanying name is a reusable catalog alias.

Shared anime aliases are written only from AnimeThemes catalog documents or
established local anime-theme mapping records. The alias writer requires an
explicit catalog flag for anime names; request snapshot names are not passed
to that writer. No network lookup is needed for capture, search, or backfill.

On the first startup with this fix, the `catalog-only-anime-aliases-v1`
migration removes all old shared anime aliases and reconstructs them from
local anime-theme mapping records and cached AnimeThemes detail documents.
The old table cannot distinguish catalog aliases from request-supplied names,
so names whose trusted source is no longer available are conservatively
discarded. Request-row names and artist/release-group aliases are retained.
The migration and its completion marker share the database transaction;
subsequent startups preserve newly captured trusted aliases even after cache
cleanup. The migration does not call upstream services.

Release-group artist associations use explicit artist identifiers before
cached/local release-group credits. Name inference runs only when neither
source establishes an identity, and requires exactly one local candidate.

Requests pagination accepts positive ASCII decimal numbers without leading
zeroes, signs, whitespace, separators, or exponents. Existing SQLite offset
bounds still apply.
