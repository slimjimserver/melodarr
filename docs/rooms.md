# Rooms: shared Plex queue synchronization

Plexamp ⇄ PMS PlayQueue ⇄ Melodarr Room

A Room synchronizes Melodarr with the host's active Plex Media Server PlayQueue.
PMS is authoritative for materialized songs and their order. Plexamp additions,
removals, reorders, and Autoplay additions are reflected in Rooms; Melodarr
requests and host edits are written back to PMS. Queue items are treated alike,
with no source labels or fabricated requester information.

## Starting and using a Room

Start Plexamp playback with at least one more song in Up Next, open **Rooms**,
and choose **Start Room**. The host needs a linked Plex account and active
Plexamp playback. A single eligible device starts directly. When multiple
devices are playing, choose the phone/computer whose queue the Room should use.
A queue with just current A and next B is sufficient.
Starting imports the existing upcoming queue without changing PMS: A remains
Now Playing, B is **Up Next · Locked**, and C/D/E keep their PMS positions.
Imported upcoming entries, including B, receive durable Room entry IDs, exact
Plex queue item IDs/rating keys, safe title/artist/album metadata, and Ready
state. A recording MBID or requester is not required.
Room JSON responses and SSE snapshots include `queue[].recordingMbid` for
comparing the recording with the recording availability API. Imported PMS items
resolve their recording MBID from the local library index using the Room's Plex
server and the track's rating key. Synchronization also fills missing MBIDs on
existing imports. Saved request MBIDs are preserved; unknown mappings stay `null`.

Use **Invite** in the Room header to share its QR or copy the secure
`/rooms/<code>?invite=<token>` URL. A code alone cannot admit a new guest.
Guests join with an optional name,
search, and request songs without Melodarr/Plex accounts. Blank names become
Guest #1, Guest #2, etc.; returning guests reuse their scoped identity. Only
the host can reorder, remove, retry synchronization, or end the Room. Ending
leaves Plexamp and its queue running; Rooms remain active until ended.

The current item comes from the original device's active Plexamp stream and
recent PMS playback notification. Up Next is the first item immediately after
it in the complete PMS queue. Both are protected from normal Melodarr edits.
When the host changes Up Next in Plexamp, passive synchronization adopts and
locks that new item. A consumed startup handoff ID stays cleared; legacy
handoff/trim fields are compatibility data, never instructions to delete or
restore the original queue suffix.

Plexamp clients may display PMS writes when playback advances. Playback controls
stay in Plexamp. Keep playable songs queued: pending requests cannot prevent
exhaustion, and Rooms cannot start/restart an idle player.

## Player presentation and cached artwork

Invite, Retry synchronization and End Room sit beside the compact Room heading,
wrapping on narrow screens. End Room uses quiet destructive text; sync errors
can emphasize Retry without coloring the whole header. Join/request successes
use a short success line, genuine failures retain danger styling, and normal
SSE connection status is hidden. There is no explanatory footer below the queue.

The Invite dialog draws a 200–240px SVG QR locally with bundled
`qrcode-generator` 2.0.4. It encodes exactly
`window.location.origin + /rooms/<code>?invite=<token>`; the copy button uses the
same URL. No external QR service receives it. Clipboard fallback exposes a
selectable full link. The dialog supports keyboard dismissal and focus return.
The guest captures the bootstrap token, removes it from the visible address
with `replaceState`, and sends it only in the join JSON, never the SSE URL.

Now Playing uses a responsive square cover with title, track artist and album.
Desktop places a 280px cover beside the metadata; mobile stacks a cover up to
360px above centered metadata. Up Next uses compact 52px mobile / 60px desktop
covers, quiet lifecycle pills and small host reorder/remove buttons. The next
live item has an **Up Next · Locked** badge and disabled host controls. Unknown
requesters are omitted. Guests keep Requested/Ready presentation and the same
layout, without host controls or detailed acquisition errors.

The host's three-dot queue menu offers **Move to top** and **Move to bottom**.
Top places the selected entry immediately after the locked live Up Next song
(or first in the Room list when Up Next is shown separately); bottom places it
last. Each shortcut submits one full order through the existing versioned
reorder API, preserving other entries' relative order and duplicate identities.
Locked entries cannot use these actions, and an action is disabled when the
entry is already at that end. The menu supports keyboard focus and Escape.

Queue-shortcut validation adds seven browser regressions for top/bottom moves,
duplicates, separate Up Next, keyboard dismissal, live lock changes, stale
versions and mobile sizing: 49 Rooms browser tests, 233 full browser tests and
five existing backend reorder regressions passed. Frontend typecheck,
production build and `git diff --check` passed. Backend synchronization is
unchanged, and no commits or pushes were made.

Artwork identity resolves locally through `track_search_plex_tracks` using the
Room server and rating key, then `albumRatingKey` and the Plex library cache's
`releaseGroupsByRatingKey` album/thumbnail. The existing Plex library scan worker
warms album thumbnails into `backend.artwork_cache`'s existing disk cache,
resizing, negative-cache and eviction system. There was no existing Plex album
image route; the Room-scoped `/api/rooms/<code>/plex-artwork/<opaque-key>` route
serves cached files only. Image renders and snapshot serialization never fetch
PMS artwork. Plex authentication stays in worker request headers.

Materialized items prefer cached Plex album art, then the existing Room
release-group artwork route, then a fixed square placeholder. Pending requests
use release-group art until they materialize. `nowPlaying`, `upNext`, `handoff`
and queue entries carry additive `artwork` / `artworkFallback` URLs; Plex IDs
remain internal and `queue[].recordingMbid` is retained. Successful image
responses have private browser caching, normal dimensions and revision-based
Plex ETags. Covers load eagerly in the hero and lazily in the queue.

The existing `room` SSE channel also publishes artwork changes when the queue
revision is unchanged. The frontend updates existing hero/entry DOM nodes by
entry ID, retaining unchanged controls, covers and keyboard focus. Duplicate
queue instances keep independent rows. Acquisition, queue reconciliation,
protected boundaries and recovery decisions are unchanged.

Player redesign validation:

| Check | Result |
| --- | --- |
| Focused player browser tests | 14 passed |
| Existing Rooms browser tests | 17 passed |
| Full browser suite | 215 passed |
| Complete Rooms backend plus artwork/cache/Plex index/worker/factory regressions | 229 passed |
| Frontend typecheck and production build | Passed |
| Ruff on changed backend modules and new artwork tests | Passed |
| Ruff formatting on changed modules/tests, with range formatting for the index helper | Passed |
| Repository Python correctness rules (`E9,F63,F7,F82`) | Passed |
| `git diff --check` | Passed |

Fourteen new browser tests and fourteen new backend tests cover hero metadata,
cached art precedence, pending materialization, missing/failed artwork, scoped
image access and caching, protected controls, guest projection, null requesters,
SSE updates without replacement, independent duplicate entries, and Unicode
layouts at 320px/390px. Desktop and mobile screenshots were visually inspected.
Backend PMS/provider transport and browser snapshots are fixtures; no live Plex
player was controlled during validation.

## Passive synchronization and saved writes

The existing worker reconciles active Rooms every five seconds. Local development
needs `python -m backend.worker` alongside the web app. Each pass resolves the
host's current stream on the original device, loads the complete active PMS
queue, verifies its identity, and updates playback/Up Next. Interrupted append
journals are recovered before external changes are interpreted.

Every pass adopts the materialized PMS projection before attempting saved writes:

- New future queue item IDs become persisted Ready Room entries in PMS order.
  A batch of Autoplay songs is handled like any other queue additions.
- Still-upcoming materialized entries missing from PMS become tombstones and
  are not re-added. Playback observation classifies current/played entries first,
  so ordinary advancement does not become an external deletion.
- Logical materialized positions follow PMS, including a changed Up Next.
  Identical rating keys with distinct `playQueueItemID`s remain independent.
  Existing MBIDs, guest IDs, and requesters stay on their exact queue instances.
- Pending unmaterialized requests survive because absence from PMS is expected.
- Changed state increments the Room revision, driving existing SSE snapshots.
  An unchanged queue produces no new revision or PMS write.

A Melodarr host reorder/remove persists exact mutation intent in
`rooms.write_intent`: queue item IDs to remove, the known materialized subset
whose relative order changed, and per-entry placement anchors. `write_pending`
is a summary of that journal, never a gate on passive adoption. Logical pending
positions and `dirty` alone do not authorize restoring the whole queue.

Recovery first observes the live baseline and recovers interrupted appends by
`add_before` IDs and rating key. It then adopts external changes, refreshes local
acquisition, and attempts individual applicable operations:

- An already absent or consumed remove is satisfied and cleared. A remove that
  became Up Next waits without hiding or deleting the protected instance.
- A saved reorder applies only to its surviving editable subset. External
  instances retain their live slots; current and the new Up Next stay protected.
- Each append/placement has its own anchors. A protected placement waits while
  unrelated queue adoption and other legal placements continue.
- An interrupted append with multiple possible new instances remains an unsafe
  Room-wide error. Ordinary duplicate tracks remain distinct by queue item ID.

Every actual PMS command still reloads the live stream and complete queue,
checks session/current/next, target/anchor and expected queue IDs, and aborts
safely on a race. A confirming read must match the predicted sequence before
that operation is cleared or its entry becomes normal Ready. Ineffective moves
or deletes remain errors. A completed passive pass clears old `sync_error`
even when an individual operation is deferred, and unchanged passes remain
idempotent.

## Retention and maintenance

Ending a Room marks it closed without modifying PMS. Closed Rooms retain their
entries, guests, tombstones, and recovery/write journals for seven days
for troubleshooting. The existing Rooms worker runs maintenance on startup and
hourly thereafter, deleting at most 25 sufficiently old closed Rooms per pass.
Foreign-key cascades delete their Room-local dependents and journals. Closed
Rooms without a known closing timestamp are retained conservatively.

Active Rooms are never eligible for deletion, regardless of age, idle playback,
missing notifications, or a disconnected device. They remain recoverable until
the host explicitly ends them. Global recording acquisitions, their requester
associations, and request histories survive Room cleanup.

Search choices expire after one hour. Maintenance removes up to 500 expired
choices and 500 obsolete rate-limit rows per pass, including choices belonging
to active Rooms. These choices are temporary search capabilities, not accepted
requests or recovery data. Search uses the same bounded expired-choice pruning. Retention, expiry,
batch sizes, and the maintenance interval are named constants in the existing
Rooms service/worker. Repeated maintenance is harmless.

## Host diagnostics and session discovery

`GET /api/rooms/<code>/diagnostics` is private to that Room's signed-in host,
including during closed-Room retention. Guests, anonymous callers, unrelated
signed-in users (including administrators), and automation API keys cannot use
it. Responses use the Rooms API's `no-store` policy.

The response explicitly allowlists:

- Room UUID/code, revision/status, dirty flag, server/client/session/queue IDs,
  current/next item IDs, playback state, saved device name/product/platform,
  and a sanitized synchronization error indicator.
- Read-only PMS queue items with exact `playQueueItemId`, `ratingKey`, safe
  title/artist/album metadata, current/next IDs, reachability and a safe error.
- All saved entries, including played/removed entries and duplicates: Room
  entry UUID, position, recording/release-group MBIDs, Plex identities, metadata,
  requester, real lifecycle/playback state, removal/lock flags, creation time,
  append journal IDs, protected-boundary deferral and a safe error indicator.
- Pending remove/order/placement intent and legacy trim IDs. Unknown JSON
  fields and invalid identity values are omitted rather than dumping journals.
- Each entry's exact recording lifecycle status, availability and Plex copy
  count, using the existing local batch lifecycle service.

Diagnostics does not reconcile, observe/persist playback, initiate acquisition,
retry writes, change order, or increment revisions. PMS/lifecycle failures still
return persisted diagnostics with generic error messages. Credentials, guest
token hashes/CSRF tokens, cookies, private provider payloads, raw provider errors
and exception traces are excluded. Older Rooms retain their original binding;
device labels default to empty when they predate this migration.

`GET /api/rooms/sessions` is read-only and requires a signed-in user. It lists
only that user's playing Plexamp music sessions, with safe device labels,
platform/product, track metadata and a stable server/device selection ID.
Queue/current IDs are included when matching playback notifications are already
available. Paused playback remains supported for an existing Room, as before.

`POST /api/rooms` accepts an optional JSON body `{"sessionId": "..."}`. With no
selection, zero sessions produces the existing playback guidance, one starts
directly, and multiple return HTTP 409 with `selectionRequired: true` and current
choices. The minimal device picker submits the selected ID and can refresh via
the discovery endpoint. A stale selection returns refreshed choices and never
falls back to a different device.

The ID binds the configured Plex server and client identity, not a stream key.
Startup rechecks ownership, the live stream, complete queue, current item and
immediate next item before saving the Room. Track advancement or a rotated
stream key between discovery and creation can be accepted on that device.
Equivalent duplicate session observations are deduplicated; genuinely separate
devices remain separate choices. Once started, existing device-bound stream
rediscovery and queue-switch protection apply.

## Lifecycle and guest presentation

The authoritative recording lifecycle still owns acquisition readiness. Hosts
see `requested`, `queued`, `downloading`, `waiting_for_plex`,
`waiting_for_queue`, and `ready`; transitions can skip intermediate states.
Exact Plex availability overrides a stale acquisition status. An available
recording awaiting this PlayQueue's confirmation is `waiting_for_queue`, and
only a confirmed materialized queue instance becomes `ready`.

A temporary local-index gap does not demote a previously Plex-ready pending
entry to `requested` or initiate acquisition again. Live recording readiness
and playable copies are still required to retry its queue insertion. Locked
Up Next deferrals retry automatically after the boundary advances, while other
PMS changes continue to synchronize.

Guests receive a pure presentation of the same authoritative snapshot: every
intermediate acquisition/materialization state is `requested`, and confirmed
entries are `ready`. This applies to join, add, GET, search and SSE responses.
It changes neither persisted state nor revisions. Host responses and private
diagnostics preserve full detail; reconciliation has one shared implementation.

`dirty`/`sync_error` alone never authorize restoring old Room order. A temporary
observation failure therefore cannot create a fight with later Plexamp edits.
Queue/server switches, duplicate item IDs, incomplete/unplayable PMS responses,
unavailable notifications, and stale owned streams fail closed.

The existing per-server/playQueue file lock serializes worker and request writes
across processes. SQLite transactions protect persisted ordering and revisions
without holding database writes during provider calls. Host APIs use optimistic
revisions and re-observe the locked boundary before validating edits.

PMS appends use `next=0` and may insert at the end of Plex's manual queue region.
When that region is empty, the existing in-place initialization promotes the
same Up Next instance after current, verifies all IDs/order/boundaries remain
unchanged and the manual-region marker is confirmed, then appends. It never
replaces the next item. Subsequent placement stays behind Up Next. With no next
item, the first appended request becomes locked. The marker behavior still
needs verification against the actual PMS version.

## Persistence and acquisition

`backend/room_storage.py` runs through the normal `storage.init_db` migration.
The idempotent migration adds `rooms.write_pending`, `rooms.write_intent`,
`room_entries.album`, and `room_entries.deferred_until`.
`recording_mbid` and `requester` now allow NULL. SQLite requires a transactional
child-table rebuild to relax the old NOT NULL constraints; saved IDs, MBIDs,
requesters, guest foreign keys, playback, tombstones, append journals, and the
ordering index are retained. Legacy dirty Rooms with entries retain their flag
on upgrade; the next reconciliation captures identifiable operations into the
new journal before adopting PMS. No origin/source column or sentinel UUID is added.

Missing songs still reserve a logical position and use the existing
`recording_requests` lifecycle: Requested → Queued → Downloading → Waiting for
Plex → Ready · Waiting for queue → Ready. Waiting for Plex means the exact
recording is absent from the Plex index. `waiting_for_queue` means the recording
lifecycle reports Ready but placement has not been confirmed in this queue.
The host sponsors guest acquisition through the existing request
history and notification path. Removing a Room entry never cancels shared
acquisition. The acquisition bridge continues during PMS outages. Already
materialized songs use PMS readiness without depending on acquisition-cache
availability. Every reconciliation refreshes unmaterialized and unconfirmed
MBID-backed entries; an interrupted placement is not excluded merely because
it already has a queue item ID. The frontend omits the requester line when no
requester exists.
PMS artwork URLs are not exposed to guests; safe album metadata is retained,
while existing release-group artwork continues for known requests.

### Pending ordering follow-up

Pending requests follow their next surviving materialized anchor, or the tail.
External reorders never fail solely because pending anchors moved. A raw pending
position before live Up Next is valid Room intent, although it cannot be filled
at that point. The snapshot displays the actual locked item first; boundary
validation uses its materialized identity rather than the first database row.

When a pending reservation lies before Up Next, `deferred_until` remembers that
protected instance. The entry stays Requested/Queued/Downloading/Waiting for
Plex or Ready · Waiting for queue according to the authoritative lifecycle.
Worker ticks continue adopting Plexamp additions, removals, reorders, and Play
Next. Once the boundary changes, the blocked pending group rebases just below
new Up Next (or current if there is no next) and available entries materialize
normally. This deliberately gives the live protected boundary priority over
perfect preservation of pending placement through arbitrary simultaneous edits.

### Root-cause trace and acquisition evidence

The previous `_adopt` could preserve a placeholder ahead of an anchor that
became live Up Next, while the public snapshot sorted that locked anchor first.
`_logical_boundary` rejected the underlying first row. `_sync` set `write_pending`
before this rejection, so retries skipped adoption and its unknown-item guard
rejected subsequent external additions. A separate pending-anchor reorder guard
also prevented passive reconciliation. These guards are removed; protected
network-write checks remain.

The normal recording status endpoint delegates to the same `recording_states`
function used by Rooms. In the inspected previous code a Ready result became
Waiting for Plex, not Requested. Requested was produced from authoritative
`not_requested`, remained stale on excluded entries with a queue item ID, or
was the frontend fallback for an unknown state. The local database had no Rooms
and no data for recording `bd9fd6a1-d41b-4b82-9ead-a4f958749a77`; consequently
its exact live transition cannot be attributed to one of those paths from local
evidence. The regression now uses that exact MBID and Plex copy `270909` with
the real indexed lifecycle, seeds a stale Requested Room row ahead of locked
Up Next, and verifies Waiting for queue followed by confirmed Ready on playback
advancement. No separate acquisition state machine or alternate readiness
source is introduced.

## Implementation

| Responsibility | Files |
| --- | --- |
| Schema and idempotent migration | `backend/room_storage.py` |
| Shared queue synchronization and write journals | `backend/services/rooms.py` |
| Owned stream discovery, notifications, PMS REST | `backend/services/plex_rooms.py` |
| Permissions, guest capabilities, SSE and safe artwork | `backend/routes/rooms.py` |
| Read-only host diagnostics | `backend/services/room_diagnostics.py` |
| Periodic reconciliation and bounded maintenance | `backend/workers/rooms.py` |
| Host/guest presentation and device picker | `frontend/src/rooms.ts` |
| Regressions | `tests/test_rooms.py`, `tests/test_room_hardening.py`, `frontend/tests/rooms.spec.ts` |

## HTTP routes

All routes are under `/api/rooms`; automation API keys grant no Room authority.

| Method and path | Access / purpose |
| --- | --- |
| GET `/active` | Signed-in host's active Room |
| GET `/sessions` | Signed-in user discovers their active Plexamp devices |
| POST `/api/rooms` | Signed-in host starts Room; optional `{sessionId}` |
| GET `/<code>/diagnostics` | That signed-in host only; read-only private diagnostics |
| POST `/<code>/join` | Public; creates/reuses scoped guest identity |
| GET `/<code>` | Host or joined guest; safe state |
| GET `/<code>/search?q=...` | Participant; existing track search |
| POST `/<code>/entries` | Participant; opaque `{choiceId}` |
| PUT `/<code>/order` | Host; `{entryIds, version}` for all upcoming entries |
| DELETE `/<code>/entries/<entry_id>` | Host; `{version}` |
| POST `/<code>/sync` | Host; retry saved intent/acquisition |
| POST `/<code>/end` | Host; close without changing playback |
| GET `/<code>/events` | Participant; SSE updates and closure |
| GET `/<code>/invite` | Owning signed-in host; secure `invitePath`, no-store |
| GET `/<code>/artwork/<release-group-mbid>` | Participant; scoped artwork |

JSON start/join/entry/order/delete requests require `Content-Type: application/json`
and `X-Room-Request: 1`. Host mutations require the existing `X-CSRF-Token`;
guest additions require the `X-Room-CSRF` returned by join. The random guest
capability is in an HttpOnly, SameSite=Lax cookie scoped to the Room API path.
For HTTPS, enable the existing `MELODARR_COOKIE_SECURE=true` setting. Reverse
proxies must preserve the public Host header or configure the Melodarr
Application URL; Origin checks support upstream TLS termination. CORS is disabled.

## Security and limits

- New codes contain four random uppercase characters from
  `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`. They identify a Room; they are not access
  secrets. The database enforces active-code uniqueness; startup retries code
  collisions up to 20 times and returns a safe 503 after exhaustion. Host/queue
  conflicts retain their existing 409 behavior. Retained closed Rooms may share
  a code with a new active Room; lookups prefer the active Room.
- Each Room has a separate 256-bit invitation capability: a cryptographically
  random 32-byte nonce is combined with the Room UUID using domain-separated
  HMAC-SHA256 and the persistent server session key. The resulting 43-character
  URL-safe token is not stored raw. Only its SHA256 verifier and nonce are saved;
  recovering it for the host also requires the persistent server key. Keep that
  key when restarting/restoring Melodarr. Changing it fails sharing safely
  without silently replacing an active invitation.
- Anonymous initial join requires a matching invite. Existing scoped guest
  cookies continue to authenticate and rejoin without it; the authenticated
  owning host may also join/access without it. Non-owner accounts cannot bypass
  this check. Invitations never replace the guest identity or CSRF system.
- Only the owner-only invite API returns the secure path. Ordinary host/guest
  JSON, SSE and diagnostics omit token, nonce, verifier and full invite URL.
  Production access logs omit queries and Referer; development Room access
  logs redact queries, including encoded invite parameter names.
- Existing active ten-character codes, UUIDs, queue bindings, entries and guest
  credentials are preserved. Migration atomically rebuilds the legacy global
  code constraint into an active-only index and checks all foreign keys before
  commit; it neither rewrites codes nor initializes legacy invites. The first
  owner Invite request initializes a legacy invite once; further requests and
  restarts retain it. Old bare links no longer admit unjoined guests; the host
  must share the new secure link. Authorized snapshots/SSE stay bound to their
  original Room UUID, so reused codes cannot redirect an old guest stream.
- Durable rate limits use peer IP and participant identity; forwarded IP headers
  are not trusted. Search permits 12/person/minute and 25/IP/minute; requests
  permit 15/person/minute and 30/IP/minute. Join permits 10/IP/minute.
- Guests receive allowlisted display data and opaque entry/search IDs. PMS
  credentials, device addresses, client/session/queue/item IDs, rating keys,
  acquisition paths, automation keys, and raw provider errors are not serialized.
- Bodies reject unknown fields; choices/tokens are scoped to one Room. Guests
  cannot supply URLs, addresses, or Plex identifiers. Server URLs use configured
  PMS settings and redirect rejection. Cross-origin JSON mutations are rejected.
- Guest documents use `frame-ancestors 'none'`; Room JSON/SSE and errors use no-store
  and no-referrer. Successful artwork responses use private browser caching.
  SSE uses participant authorization, ends on closure, and allows
  at most six bounded streams per process. Reverse proxies must disable buffering.
- Guest presence counts guests joined, not online presence. Guest additions are
  limited to 200 upcoming entries, guests to 500. Imported PMS items are retained;
  host reorder validation uses the 10,000-item PMS queue window rather than the
  guest request limit. Complete reads are required; truncated queues are rejected.
- Playback can race a network command. Owned stream and complete queue are checked
  before every mutation; changed protected boundaries stop the write. Voting
  and guest reordering remain outside this iteration.

## Validation for this iteration

| Check | Result |
| --- | --- |
| Targeted hardening and two affected guest SSE regressions | 31 passed |
| Full Rooms backend: `python -m unittest tests.test_rooms tests.test_room_hardening` | 142 passed |
| Full backend: `python -m unittest discover -s tests -t .` | 1,092 passed |
| Targeted browser: `pnpm run test:browser tests/rooms.spec.ts` | 17 passed |
| Full browser: `pnpm run test:browser` | 201 passed |
| Frontend: `pnpm run check`, `pnpm run build` | Passed |
| Ruff on Rooms modules/tests; repository `E9,F63,F7,F82`; changed Python formatting | Passed |
| `git diff --check` | Passed |

This iteration adds 29 backend tests for bounded cleanup, FK cascades, global
request preservation, read-only diagnostics, secret exclusion, exact lifecycle
transitions, guest JSON/SSE projection and multi-device selection/revalidation.
Three browser regressions cover device selection, a vanished selection without
fallback, refresh to an empty device list, and guest Requested-to-Ready updates.
The factory inventory now explicitly verifies both added endpoints. The large
legacy `test_backend.py` has the same 12 existing general Ruff findings as HEAD;
this task introduces none, and repository correctness checks pass. Its four-line
route-inventory assertion update does not reformat unrelated tests.

Existing acquisition, duplicate identity, synchronization/write-race, migration,
security, concurrency and recovery regressions remain in the suites. Live
restart/recovery was already manually validated and was not redesigned.

## Invitation and header polish validation

| Check | Result |
| --- | --- |
| Focused invitation/security/migration backend tests | 21 passed |
| Focused invitation plus test-filesystem isolation checks | 24 passed |
| Full Rooms backend (`tests.test_rooms`, `test_room_hardening`, `test_room_artwork`, `test_room_invitations`) | 177 passed |
| Full backend (`python -m unittest discover -s tests -t .`) | 1,127 passed |
| Focused invite browser tests | 11 passed |
| Full Rooms browser (`rooms.spec.ts`, `rooms-player.spec.ts`, `rooms-invitations.spec.ts`) | 42 passed |
| Full browser suite | 226 passed |
| Frontend typecheck and production build | Passed |
| Ruff on touched backend modules and Rooms tests | Passed |
| Repository `E9,F63,F7,F82` static checks | Passed |
| Ruff formatting, with changed-range checks for legacy factory/storage/test inventory files | Passed |
| `git diff --check` | Passed |

The 21 new backend tests cover code/alphabet/collision behavior, bounded failure,
closed-code reuse, hash storage, invite authorization and owner-only sharing,
guest cookie/CSRF continuity, secret-free JSON/SSE/diagnostics/logs, stable restart
and legacy invites, UUID binding for streams and completing requests, preserved
migration children, and migration rollback. Eleven new browser tests decode the
rendered QR with test-only `jsqr` 1.4.0, check exact secure copying and clipboard
fallback, keyboard focus, guest success/error styling, header controls and mobile
fit. The artwork test now explicitly imports the existing isolation bootstrap.
The legacy `test_backend.py` retains its same 12 unrelated Ruff findings as HEAD;
the conventional Gunicorn config filename is exempted from `N999`.
No commits or pushes were made. PMS synchronization decisions were unchanged;
automated PMS coverage uses the existing transport mocks.

## Real PMS/Plexamp acceptance checks

Backend tests mock PMS transport; browser tests use fixture snapshots. Before
deployment, exercise live PMS with local and remote/cellular Plexamp:

1. Start with A/B and A/B/C/D/E; confirm all IDs/order survive and B locks.
2. Append single/batched songs, allow Autoplay, insert duplicates, remove only one
   duplicate, reorder future items, and change Up Next in Plexamp. Confirm Rooms
   converges without restorative PMS writes.
3. Add/reorder/remove through Melodarr; verify PMS order, remote-client refresh
   timing, and manual-region initialization on the deployed PMS version.
4. Advance across stream keys, retain/prune history, pause/resume on the original
   device, and disconnect/reconnect notifications. Switch queues/servers and
   confirm safe errors. End a Room while music continues.
5. Interrupt append/move/delete and restart Melodarr; verify recovery, protected
   boundaries, and adoption of unrelated external changes and safe failure on ambiguous appends.
   Acquire a missing recording through Lidarr/Plex and verify late readiness and
   deferred placement that retries after playback advances.
6. Verify SSE add/remove/reorder through the HTTPS proxy, scoped guest cookies,
   near-empty warnings, and exhausted-player behavior.
7. With phone and PC both playing, select each device in separate Rooms and
   compare the private diagnostics binding with its live queue. Stop the selected
   device or switch its queue; confirm it never falls back to the other device.
