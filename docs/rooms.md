# Rooms: shared Plex queue synchronization

Plexamp ⇄ PMS PlayQueue ⇄ Melodarr Room

A Room synchronizes Melodarr with the host's active Plex Media Server PlayQueue.
PMS is authoritative for materialized songs and their order. Plexamp additions,
removals, reorders, and Autoplay additions are reflected in Rooms; Melodarr
requests and host edits are written back to PMS. Queue items are treated alike,
with no source labels or fabricated requester information.

## Starting and using a Room

Start Plexamp playback with at least one more song in Up Next, open **Rooms**,
and choose **Start Room**. The host needs a linked Plex account and exactly one
active Plexamp device. A queue with just current A and next B is sufficient.
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

Share the Room code or `/rooms/<code>` URL. Guests join with an optional name,
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
| Periodic acquisition/playback reconciliation | `backend/workers/rooms.py` |
| Existing host/guest presentation | `frontend/src/rooms.ts` |
| Regressions | `tests/test_rooms.py`, `frontend/tests/rooms.spec.ts` |

## HTTP routes

All routes are under `/api/rooms`; automation API keys grant no Room authority.

| Method and path | Access / purpose |
| --- | --- |
| GET `/active` | Signed-in host's active Room |
| POST `/api/rooms` | Signed-in host starts Room |
| POST `/<code>/join` | Public; creates/reuses scoped guest identity |
| GET `/<code>` | Host or joined guest; safe state |
| GET `/<code>/search?q=...` | Participant; existing track search |
| POST `/<code>/entries` | Participant; opaque `{choiceId}` |
| PUT `/<code>/order` | Host; `{entryIds, version}` for all upcoming entries |
| DELETE `/<code>/entries/<entry_id>` | Host; `{version}` |
| POST `/<code>/sync` | Host; retry saved intent/acquisition |
| POST `/<code>/end` | Host; close without changing playback |
| GET `/<code>/events` | Participant; SSE updates and closure |
| GET `/<code>/artwork/<release-group-mbid>` | Participant; scoped artwork |

JSON join/entry/order/delete requests require `Content-Type: application/json`
and `X-Room-Request: 1`. Host mutations require the existing `X-CSRF-Token`;
guest additions require the `X-Room-CSRF` returned by join. The random guest
capability is in an HttpOnly, SameSite=Lax cookie scoped to the Room API path.
For HTTPS, enable the existing `MELODARR_COOKIE_SECURE=true` setting. Reverse
proxies must preserve the public Host header or configure the Melodarr
Application URL; Origin checks support upstream TLS termination. CORS is disabled.

## Security and limits

- Codes contain ten cryptographically random characters (50 bits), with no public
  listing. Anyone with the invite can join; lookup/join/search/mutations are limited.
- Durable rate limits use peer IP and participant identity; forwarded IP headers
  are not trusted. Search permits 12/person/minute and 25/IP/minute; requests
  permit 15/person/minute and 30/IP/minute. Join permits 10/IP/minute.
- Guests receive allowlisted display data and opaque entry/search IDs. PMS
  credentials, device addresses, client/session/queue/item IDs, rating keys,
  acquisition paths, automation keys, and raw provider errors are not serialized.
- Bodies reject unknown fields; choices/tokens are scoped to one Room. Guests
  cannot supply URLs, addresses, or Plex identifiers. Server URLs use configured
  PMS settings and redirect rejection. Cross-origin JSON mutations are rejected.
- Guest documents use `frame-ancestors 'none'`; Room responses use no-store and
  no-referrer. SSE uses participant authorization, ends on closure, and allows
  at most six bounded streams per process. Reverse proxies must disable buffering.
- Guest presence counts guests joined, not online presence. Guest additions are
  limited to 200 upcoming entries, guests to 500. Imported PMS items are retained;
  host reorder validation uses the 10,000-item PMS queue window rather than the
  guest request limit. Complete reads are required; truncated queues are rejected.
- Playback can race a network command. Owned stream and complete queue are checked
  before every mutation; changed protected boundaries stop the write. Voting, QR
  codes, and guest reordering remain outside this iteration.

## Validation for this iteration

| Check | Result |
| --- | --- |
| Targeted backend: `python -m unittest tests.test_rooms` | 108 passed |
| Full backend: `python -m unittest discover -s tests -t . -v` | 1,058 passed |
| Targeted browser: `pnpm run test:browser tests/rooms.spec.ts` | 14 passed |
| Full browser: `pnpm run test:browser` | 198 passed |
| Frontend: `pnpm run check`, `pnpm run build` | Passed |
| Ruff on Rooms modules/tests; repository `E9,F63,F7,F82`; changed Python formatting | Passed |
| `git diff --check` | Passed |

This iteration adds ten backend regressions and one browser regression covering
the requested/download/materialized sequence, passive edits and new Up Next,
raw pending positions ahead of locked next, deferred worker retry, the exact
365 MBID with real indexed readiness, interrupted-add adoption, satisfied and
protected removes, subset reorder recovery, legacy flag recovery, missing
playable copies, SSE, and idempotence. Migration coverage exercises the new
columns on the old schema. Existing acquisition, duplicate identity, write-race,
security, and concurrency regressions remain in the suites.

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
