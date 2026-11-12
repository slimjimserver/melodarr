# Artist/request LTE browser benchmark

This opt-in measurement tool uses the existing Playwright Chromium installation
and the normal Melodarr UI: open home, search an artist, click its result, use its
discography, select one configured release group, and optionally request it.
It is outside `tests/` and is never part of the normal browser test suite.
No timing or payload-size regression budgets are enforced.

## Setup and safe first run

Run commands from `frontend/` after installing the repository's dependencies:

```sh
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
npm run build
npm run benchmark:lte:fixture
```

The fixture command starts its own HTTP server on a randomly assigned loopback
port, using the existing browser-test fixture with opt-in benchmark data. It
ignores target URL, authentication and artist/release environment settings, and
uses `Fixture Artist` / `fixture-artist` / `fixture-album`. It serves real HTTP
assets and cacheable artwork, including below-fold artwork that the application
itself lazy-loads. It does not use Playwright `route()`, synthetic API fulfillment,
artificial response sleeps, or external music providers. Authentication is seeded
only in this isolated fixture. Its request handler has no acquisition side effects
and deliberately leaves the target requestable for repeated measurements.

By default the final request is **skipped**, even in fixture mode. To prove the
entire accepted-request journey safely in PowerShell:

```powershell
$env:BENCHMARK_ALLOW_REQUEST = '1'
npm run benchmark:lte:fixture
Remove-Item Env:BENCHMARK_ALLOW_REQUEST
```

For POSIX shells, use `BENCHMARK_ALLOW_REQUEST=1 npm run benchmark:lte:fixture`.

## Configure a running Melodarr instance

There are no live credentials or music defaults in the harness. Set all four
target/content variables explicitly. The IDs must be MusicBrainz artist and
**release-group** IDs, rather than a release/edition ID.

```powershell
$env:MELODARR_BENCHMARK_URL = 'http://localhost:8686/' # replace with your instance
$env:MELODARR_BENCHMARK_STORAGE_STATE = 'C:\private\melodarr-auth.json'
$env:BENCHMARK_ARTIST_QUERY = 'Your chosen artist'
$env:BENCHMARK_ARTIST_MBID = '<artist-mbid>'
$env:BENCHMARK_RELEASE_GROUP_MBID = '<release-group-mbid>'
npm run benchmark:lte
```

The home URL accepts HTTP(S) `/` or `/discover`, without credentials, a query or a
fragment. This application uses root-relative routes. The command does not start
or reset a live server. The example port above is illustrative; use your actual
URL. `npm run benchmark:lte -- --fixture` is equivalent to the fixture command.

To create storage state, use Playwright's normal interactive authentication:

```sh
pnpm exec playwright codegen --save-storage=/private/melodarr-auth.json http://your-instance/
```

Sign in normally, then close the codegen browser. Keep this file private and
outside the repository. The benchmark reads **cookies only** from the state file:
it deliberately ignores saved localStorage and IndexedDB so the cold run does
not inherit previously viewed frontend data. Melodarr's current session uses
cookies. The authenticated home/search UI must become usable within the timeout;
an expired/missing session fails the startup stage. No credentials, storage-state
contents, headers, bodies, console logs, screenshots or traces are saved in the
benchmark artifacts. Other authentication/proxy mechanisms requiring localStorage
or extra HTTP credentials would need explicit harness support.

Select an artist returned by your query and a release group visible in the normal
discography. Search result pagination uses the UI's Show more button if needed.
For a hidden release, optional `BENCHMARK_RELEASE_SECTION` (e.g. `Other releases`)
clicks the matching discography navigation link; `BENCHMARK_RELEASE_FILTER` enters
a public release title into the normal discography search field to reveal matches
and secondary types. These actions and any resulting API traffic are measured as
selection. The harness never opens every section or scrolls through every image.

Current discography cards offer Request directly. The benchmark uses that control
by default and reports release detail as `null` / `—`. If no direct Request exists,
it clicks the target release group's detail link and measures the wait for Request
release group. Set `BENCHMARK_OPEN_RELEASE_DETAIL=1` to deliberately measure that
alternative UI path; it is identified in the JSON configuration.

## Request safety and repeated runs

Without **exactly** `BENCHMARK_ALLOW_REQUEST=1`, the harness stops at the usable
Request control, records `requestStatus: "skipped"`, and leaves `requestMs` and
`totalJourneyMs` null. `timeToRequestControlMs` is the elapsed partial journey,
not a fabricated acceptance time.

For intentional testing on `melodarr-test`, set the flag explicitly before
`npm run benchmark:lte`. A URL, storage-state file, or fixture option never enables
requests. CDP Fetch interception additionally blocks every `/api/request*`
acquisition call except a single armed POST to `/api/request/release-group` on the
configured origin with the exact configured MBID. The guard stays active until
the measured document is unloaded. Unexpected requests fail the run. Fetch
interception is limited to acquisition endpoints and preserves browser caching.

Each enabled run attempts one real request: the default three profiles with two
cache scenarios can make **six** requests. A live request may make the target
Queued/Available and no longer requestable on a later run. That is a functional
failure, not a successful repeat measurement. The harness never cancels a request,
deletes server state, or chooses another release to work around this. For repeated
acceptance measurements, use a disposable/resettable test instance whose target
remains eligible, resetting request state separately and intentionally between
experiments. The default dry run measures all six scenarios without this effect.

## Profiles and cache definition

| Profile      |     Download |       Upload | Additional latency |
| ------------ | -----------: | -----------: | -----------------: |
| `baseline`   | unrestricted | unrestricted |               0 ms |
| `normal-lte` |       8 Mbps |       2 Mbps |              70 ms |
| `poor-lte`   |     1.5 Mbps |     500 Kbps |             150 ms |

Named constants are in `network-metrics.mjs`. Speeds use decimal bits per second,
converted to bytes per second for `Network.emulateNetworkConditions`. Baseline
uses CDP's unrestricted `-1` throughput. The conditions apply before navigation
to actual HTML, JS, CSS, JSON, artwork and request responses, with upload throttling
on the actual submission. They are intentionally chosen profiles, not universal
LTE speeds.

Each profile/repetition creates a fresh non-persistent browser context, imports
only authentication cookies, explicitly clears HTTP cache, and keeps cache enabled.
The **cold** run has no frontend/detail state. The **warm** run follows it in the
same page and context, without clearing cache, navigating home in a new document
after leaving the prior document. This preserves browser HTTP/artwork caching and
local storage while resetting in-memory detail state; it avoids browser Back/BFCache
and SPA-only reuse masking network costs. The first run's bounded post-usability
observation can populate additional near-viewport artwork into the warm cache.
Server metadata/MusicBrainz/Plex/artwork caches are never wiped. Browser-cold is
not server-cold; even the first profile can encounter provider/cache latency.

## Timings and progressive usability

Browser `performance.now()` records actual input/click events and readiness checks
within each document. UI actions use touch taps in the mobile context. Readiness
requires visible/enabled controls and Playwright's trial tap actionability check,
including overlay checks and scrolling only to the next intended control. Startup
also observes the existing discovery initialization event so visible markup is
not mistaken for a bound search form. There are no waits for every artwork load.

| Field                    | Start → finish                                                                                            |
| ------------------------ | --------------------------------------------------------------------------------------------------------- |
| `startupMs`              | document time origin → authenticated artist search control usable                                         |
| `searchMs`               | first artist search input event → intended artist result clickable                                        |
| `artistDetailMs`         | intended artist click event → detail title, discography and a release detail link usable                  |
| `selectionMs`            | usable discography → target Request control usable, or target release detail click                        |
| `releaseGroupMs`         | optional release detail click → Request release group usable                                              |
| `requestMs`              | target Request click event → target UI shows Requested, Queued, Available or Downloading after submission |
| `totalJourneyMs`         | first search input event → UI confirms acceptance (enabled runs only)                                     |
| `timeToRequestControlMs` | first search input event → target Request control usable (always)                                         |

The query is filled and Search submitted through the frontend; the tap clears
the application's normal typing debounce. Raw `milestonesMs` are also saved.
Timings include small automation/readiness-check overhead between UI actions and
do not include sign-in or the post-usability network observation. `startupMs` is
reported separately so HTML/bundle download cost is visible.

Home normally auto-loads recommendations. CDP `Network.setBlockedURLs` blocks
`/api/discover*` so this experiment measures only search/discography/request traffic.
Blocked recommendation requests are excluded from totals and counted separately.
The recommendation area can show an error; that area's usability is outside this
journey. The application source is unchanged. Other ordinary startup/detail work
(availability, revalidation, anime links, etc.) remains part of the measurement.

`usableNetwork` snapshots traffic at the final usable milestone. Afterwards a
bounded observation waits for zero pending HTTP requests and 500 ms without network
activity, for at most 5 seconds, and saves `observedNetwork` plus `networkQuiet`.
This wait never delays a measured action. A timeout is reported without failing
the run, and network quiet does not mean below-fold lazy images have loaded.

## Transfer accounting and artifacts

Timestamped `*-artist-request.json` and `.md` files are saved in the ignored
repository-root `benchmark-results/`. JSON preserves raw bytes and per-stage
counts; Markdown formats decimal KB/MB and includes timings, byte categories,
cache/pending/failure counts and a largest-first request inventory for every run.
Failed runs/setup errors also generate artifacts and exit nonzero. No timing or
size threshold causes a failure.

CDP `Network.requestWillBeSent`, `responseReceived`, `dataReceived`,
`requestServedFromCache`, `loadingFinished`, and `loadingFailed` are observed on
the actual page. Completed transfers use **`loadingFinished.encodedDataLength`**,
not Content-Length or decoded JSON/image sizes. Redirect hops use the observed
`redirectResponse.encodedDataLength` and count as separate HTTP requests.
Cache-served resources contribute zero network bytes, even if Chromium reports
a resource length; the raw CDP length remains in the inventory. Normal network
revalidation responses still contribute their measured transfer bytes. Pending,
canceled and failed transfers use accumulated `dataReceived.encodedDataLength`
as an explicitly labeled partial lower bound and are counted, not silently dropped.

Requests are assigned to their **initiation** stage (startup, search, discography,
selection, optional release-group, request, or after-usable), even when an artwork
request finishes in a later stage. Byte categories are artwork/images first, then
API/JSON, then JS/CSS/fonts, then HTML/other. Total HTTP request count includes
cache hits and redirect hops. Inventories omit credentials, fragments and unknown
query parameters, retain public `/api/search` values and artwork `size`, and redact
invite/token path segments. Do not use secrets as configured public search terms.

## Other options and validation

| Environment variable            | Default / purpose                                                  |
| ------------------------------- | ------------------------------------------------------------------ |
| `BENCHMARK_PROFILES`            | `baseline,normal-lte,poor-lte`; comma-separated subset             |
| `BENCHMARK_REPETITIONS`         | `1`; fresh cold/warm pairs per profile                             |
| `BENCHMARK_TIMEOUT_MS`          | `60000`; functional UI/navigation timeout                          |
| `BENCHMARK_QUIET_IDLE_MS`       | `500`; observed idle interval                                      |
| `BENCHMARK_QUIET_TIMEOUT_MS`    | `5000`; maximum post-usability observation                         |
| `BENCHMARK_OUTPUT_DIR`          | `../benchmark-results`; keep overrides outside tracked directories |
| `BENCHMARK_OPEN_RELEASE_DETAIL` | unset; exact `1` forces release detail path                        |
| `BENCHMARK_RELEASE_SECTION`     | unset; optional visible discography section label                  |
| `BENCHMARK_RELEASE_FILTER`      | unset; optional public release title/filter text                   |

Validation commands:

```sh
npm run test:benchmark
npm run benchmark:lte:fixture
# Also exercise the fixture with BENCHMARK_ALLOW_REQUEST=1 and, separately,
# BENCHMARK_OPEN_RELEASE_DETAIL=1. Never enable those on a live target by default.
npm run test:browser
npm run check
npm run build
git diff --check
```

## Limits

This simulates constrained client networking, not a physical cellular carrier.
Chromium CDP throttling cannot reproduce radio contention, signal changes, carrier
routing, packet loss/jitter, handovers, mobile CPU/memory limits or modem behavior.
Additional latency is on top of actual server/provider latency. The viewport/touch
configuration is mobile (390 × 844, DPR 2), but CPU remains the host's CPU.
CDP method support is checked by executing the commands; unsupported instrumentation
fails instead of silently yielding unthrottled results. The current emulation
method is deprecated in the tip-of-tree protocol but remains supported by the
repository's Chromium; migration can use `emulateNetworkConditionsByRule` and
`overrideNetworkState` together when changing browser versions.

Transfer numbers are encoded HTTP **response** transfers as reported by Chromium,
including protocol/header overhead when that event reports it. They do not count
uploaded body/request-header bytes, TLS/TCP/IP or cellular framing, DNS, WebSocket
frames, or provider requests made by the Melodarr server. Partial transfer sizes
can lag under throttling; prefer the bounded observed totals for complete responses.
Page CDP events are received asynchronously and snapshots/readiness checks can
include a small scheduling delay. Service workers are blocked/bypassed to avoid
unaccounted worker-side network traffic. The current Melodarr worker handles push
notifications rather than the benchmark flow. Multi-page/worker requests would
need additional instrumentation if the UI changes.

Relevant primary references: [CDP Network protocol](https://chromedevtools.github.io/devtools-protocol/tot/Network/)
and [Playwright BrowserContext routing/cache behavior](https://playwright.dev/docs/api/class-browsercontext#browser-context-route).
