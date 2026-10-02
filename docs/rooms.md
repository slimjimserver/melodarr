# Rooms MVP

The host must start Plexamp playback before starting a Room, with at least one
more song in Up Next. Open **Rooms** in Melodarr, then **Start Room**. The host
must have a linked Plex account. If multiple Plexamp devices are playing under
that account, stop the others before starting.

Melodarr manages the active Plex PlayQueue through Plex Media Server. Remote
Plexamp clients may not show queue changes immediately; they naturally
synchronize when playback advances to the next track. Playback controls stay
in Plexamp. Rooms cannot start or restart an idle/exhausted player.

The live current song and immediate next song are always protected. The initial
next song is the startup handoff buffer; the rest of the original upcoming queue
is removed. Every transition selects the new live Up Next item as locked. Room
queue management starts after it. Hosts cannot move or remove it, or place a
request ahead of it. Once the startup handoff starts playing, its saved ID is
cleared permanently. It has no later special protection; only its actual live
position can protect it. Keep playable songs queued before playback ends; pending
requests do not prevent queue exhaustion.

Share the displayed code or copy its `/rooms/<code>` URL. Guests join with an
optional name, search, and request songs without a Melodarr/Plex account.
Blank names become Guest #1, Guest #2, etc. A returning guest can click Join
again to reuse the room-scoped identity. Only the host can reorder, remove,
retry synchronization, or end a Room. Ending leaves Plexamp and its queue
running. Rooms remain active until explicitly ended.

## Implementation

| Responsibility | Files |
| --- | --- |
| Durable room/guest/entry schema | `backend/room_storage.py`, invoked by `storage.init_db` |
| Room permissions, logical order, reconciliation journal | `backend/services/rooms.py` |
| Active-session discovery, PMS notification stream and REST queue adapter | `backend/services/plex_rooms.py` |
| HTTP, guest capabilities, rate limits, SSE and artwork boundary | `backend/routes/rooms.py` |
| Acquisition/playback reconciliation loop | `backend/workers/rooms.py`, registered by `backend/worker.py` |
| Shared host/guest interface | `frontend/src/rooms.ts`, `frontend/src/style.css` |
| Host and separate public guest documents | `frontend/static/index.html`, `frontend/static/room.html`, `backend/routes/pages.py` |

Other wiring changes: `backend/application.py` registers the blueprint;
`backend/security.py` delegates only guest join/add CSRF checks to that boundary;
`backend/routes/discovery.py` exposes parameters on the existing search helper;
`backend/requirements.txt` pins the WebSocket dependency; `frontend/src/app.ts`
and `frontend/scripts/build.mjs` wire routing/lazy loading and asset builds.
`README.md`, `docs/api.md`, and `.gitignore` publish this documentation.

Coverage is added in `tests/test_rooms.py` and `frontend/tests/rooms.spec.ts`.
`tests/test_backend.py` updates route/thread assertions, and
`frontend/tests/fixture-server.mjs` plus
`frontend/tests/primary-requests-navigation.spec.ts` accommodate the public
document and the fifth navigation item. Together with the table above and
this file, these are all 27 changed/added files.

Session discovery matches the linked Plex **username** against `/status/sessions`
`User.title`, never the server-local `User.id`. PMS WebSocket notifications must
match both client identifier and the **current stream's** session key before
their queue IDs are used. Each reconciliation re-resolves `/status/sessions`
for the host on the Room's original Plexamp device; a stream key captured at
startup is not reused as a permanent device identity. The notification's Plex
rating key must also agree with the live session and queue item. See the
[stream session reference](https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference)
for the distinction between a current stream's session key and its device.
Discovery has a bounded wait and rechecks the session. Recent notifications
are required to mutate the queue safely; a queue switch requires ending and
restarting the Room. Changing the configured PMS server also requires a restart.

The adapter reuses Melodarr's requests/redirect protection rather than adding
a parallel Plex library implementation. Queue REST semantics follow the
[Python PlexAPI queue source](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/playqueue.html).
Only `websocket-client==1.9.0` is added to production requirements, which the
existing Dockerfile installs. No Companion connection is used.
PMS appends explicitly use `next=0`. Before appending when a next item exists,
the complete queue must report a `playQueueLastAddedItemID` at or beyond it.
Plex's [official PlayQueue API](https://developer.plex.tv/pms/) appends to the end
of the manual region; an empty region would insert ahead of the protected next
item. If that region is empty, Rooms first issues an in-place move of the same
next queue instance after its existing current predecessor. It then reloads PMS
and requires every queue item ID, its order, current/next IDs, and stream to stay
unchanged, with the manual-region marker now at or beyond next. Only then can
the request append. This bootstrap runs under the same mutation lock, recovers
interrupted initialization, and is skipped once the manual region exists. A PMS
response that does not confirm initialization stops the append with a saved
synchronization error. No extra manual queueing step is normally needed. The
marker change on an in-place move requires verification against the actual PMS
version; automated tests model both confirmed and ineffective responses.
If no upcoming item exists, the first appended Room item becomes locked immediately.

## Persistence and acquisition

The additive, idempotent migration adds `rooms`, `room_guests`, `room_entries`,
`room_choices`, and `room_rate_limits`, with partial unique indexes enforcing
one active Room per host and PMS queue. Existing databases migrate through the
project's normal `init_db` runner; no separate migration command is needed.
Existing Rooms tables also receive `next_item_id` and `up_next` columns through
idempotent ALTER statements. These cache the observed boundary for display;
mutations always resolve it again from live PMS playback.

File locks shared across processes serialize each server/queue. SQLite
transactions protect ordering and revisions, without holding database writes
open during provider calls. Host edits use optimistic revision checks. Each
duplicate request has its own entry ID and PMS queue item ID.

Missing songs immediately reserve a logical queue position as Requested. The
normal `recording_requests` service determines Requested/Queued/Downloading/
Waiting for Plex/Ready states. **The host sponsors guest acquisition**, using
the host's existing request history and notification semantics. The Room
preserves the guest's requester name separately. Removing a Room entry never
cancels shared acquisition.

Production's existing background-worker runner reconciles active Rooms every
five seconds. Local development needs `python -m backend.worker` alongside the
web app, as for other background jobs. Playable songs are inserted in logical
order after the live Up Next boundary; pending positions can fill later while
they remain beyond that boundary. If a later materialized request reaches Up
Next ahead of an earlier pending logical position, synchronization fails safely
until playback advances or the host restarts the Room. It never inserts a late
download ahead of an already locked item.
Normal ticks observe PMS/local lifecycle state and avoid redundant queue writes.
The acquisition bridge continues advancing requests during a PMS outage;
playable copies wait for queue synchronization to recover.

Manual **Retry synchronization** and the worker both call `rooms.reconcile`.
They refresh the active PMS stream, reconsider all upcoming placeholders via
the shared recording lifecycle, recover interrupted queue writes, and enforce
the logical order after the current and immediate-next items. A newly inserted entry stays
Waiting for Plex until a final PMS read confirms its queue item and position.
The committed Room revision drives the existing SSE updates.

The playback correction addresses two reproduced backend failures: matching
every notification against the startup stream key left the old current/handoff
state in use after a stream change; retaining a consumed handoff item ID allowed
queue history to resurrect it as an anchor. Stream ownership/device matching is
now refreshed before queue writes, and a consumed handoff is stored as an empty
anchor. The dynamic Up Next correction adds the two display columns above. A changed PMS
queue still requires ending the Room and starting another; it is never silently
adopted or written using an older stream notification.

The actual next item is the first complete PMS queue item immediately after the
notification's current `playQueueItemID`. The current item's rating key must
agree with the owned active stream. Host reorder/remove APIs enforce the boundary
inside the same server/queue file lock used by worker reconciliation. Every PMS write
reloads the full queue and live stream, checking both protected IDs, the stream
key, the target, the move anchor, and the expected item sequence. Drift stops the
pass with a persisted synchronization error. Invalid host attempts return 409;
an inconsistent live/database boundary returns 502 without changing queue intent.

After recovering append journals, the reconciler builds the expected suffix from
active materialized Room entries by exact queue item ID. It removes every other
item beyond Up Next, including foreign instances with the same recording or
tracks interspersed between requests, then orders the Room instances. Unexpected
current/next items and played history are preserved. A final read must confirm
the entire suffix; an already correct queue receives no PMS mutations.

Public snapshots expose allowlisted `upNext` track metadata and a per-entry
`locked` flag. A matching Room entry is shown first with **Up Next · Locked**;
all three controls are disabled, and the first unlocked entry cannot move above
it. An unowned next song uses the existing track card. No PMS identifiers are
exposed. The legacy `handoff` field remains only for startup compatibility.

PMS and SQLite cannot share a transaction. Intent is committed first, removals
use tombstones, and interrupted appends retain the pre-add queue IDs so retries
can recover the exact new instance without duplicating it. Failures return an
error and remain visible in room state. Ambiguous external edits fail closed
and may require a host restart. The host can retry acquisition failures.

## HTTP routes

All routes are under `/api/rooms`; automation API keys grant no Room authority.

| Method and path | Access / purpose |
| --- | --- |
| GET `/active` | Signed-in host's active Room |
| POST `/api/rooms` (base path) | Signed-in host starts Room |
| POST `/<code>/join` | Public, creates/reuses scoped guest identity |
| GET `/<code>` | Host or joined guest, safe room state |
| GET `/<code>/search?q=...` | Participant; reuses `/api/v1/search` track implementation |
| POST `/<code>/entries` | Participant; body `{choiceId}` from that Room's search |
| PUT `/<code>/order` | Host; `{entryIds, version}` for all upcoming entries |
| DELETE `/<code>/entries/<entry_id>` | Host; `{version}` |
| POST `/<code>/sync` | Host; retry saved intent/acquisition |
| POST `/<code>/end` | Host; close Room without changing playback |
| GET `/<code>/events` | Participant; SSE state updates and final closure |
| GET `/<code>/artwork/<release-group-mbid>` | Participant; artwork for Room search/entries only |

JSON mutation requests for join/entries/order/delete require
`Content-Type: application/json` and `X-Room-Request: 1`. Host mutations require
the existing `X-CSRF-Token`; guest additions require `X-Room-CSRF`, returned by
join. The random guest capability is in an HttpOnly, SameSite=Lax cookie scoped
to the Room API path. Production HTTPS installations should enable the existing
`MELODARR_COOKIE_SECURE=true` setting.

For reverse proxies, preserve the public Host header or configure the existing
Melodarr Application URL setting. Origin checks support upstream TLS termination;
the JSON/custom-header requirement and disabled CORS prevent browser cross-origin
mutation requests.

## Security and MVP limits

- Codes contain ten cryptographically random characters (50 bits).
  They are invite capabilities: anyone with a shared link can join that Room.
  There is no public room listing. Lookup/join/search/mutation rates are limited.
- Rate limits use durable SQLite counters for both peer IP and guest identity.
  Forwarded IP headers are not trusted; guests behind one proxy/NAT share an
  IP budget. Search permits 12 queries/person/minute and 25/IP/minute; requests
  permit 15/person/minute and 30/IP/minute. Join permits 10/IP/minute.
- Guests receive allowlisted display data and opaque entry/search IDs. PMS
  credentials, device addresses, client/session/queue/item IDs, rating keys,
  acquisition paths, automation keys and raw provider errors are never serialized.
- Request bodies reject unknown fields. Choices and guest tokens are scoped
  to one room. Guests cannot supply URLs, network addresses or Plex identifiers.
  Server-only URLs use configured PMS settings and redirect rejection.
- Cross-origin JSON mutations are rejected. Guest-only documents carry
  `frame-ancestors 'none'`; Room responses use no-store and no-referrer.
  SSE requires the same participant authorization as state reads and ends on
  room closure. Guest credentials stop authorizing mutations after closure.
- SSE uses bounded streams and at most six concurrent streams per process,
  reserving Flask's remaining threads for ordinary requests. Extra browsers
  retry with backoff. Reverse proxies must allow SSE and disable buffering.
  Guest presence is reported as total guests joined, not live online presence.
- QR codes, voting and guest reorder permissions are omitted. Upcoming Room
  entries are capped at 200 and guests at 500. Queue reads must return the
  complete PMS queue; very large/truncated queues are rejected.
- A track transition can race a network command. The owned stream and complete
  queue are checked immediately before each mutation, and boundary changes stop the
  current reconciliation. Only room-requested upcoming items are reordered.

## Validation results

- Full Python backend suite: **1,032 tests passed**, including **82 Rooms tests**.
- Full Playwright browser suite: **190 tests passed**, including **10 Rooms tests**.
- Frontend type checks (`pnpm run check`) and production build (`pnpm run build`)
  passed.
- Targeted Rooms backend suite: **82 tests passed**; the full browser suite includes
  **10 Rooms tests**.
- Ruff checks passed for all new Rooms Python modules and tests, plus the
  repository-wide `E9,F63,F7,F82` checks. `git diff --check` passed.
- Docker was unavailable in the development environment, so an image build
  remains unverified. No dependency vulnerability scanner was run.
- PMS and browser interactions were mocked in automated tests; real PMS and
  remote/cellular Plexamp validation remains outstanding as listed below.

Five focused pending-recording regression tests use the real indexed availability
and recording lifecycle with mocked PMS transport. They cover manual retry from
Waiting for Plex to Ready, insertion before an existing later song, saved queue
item IDs, SSE updates, repeat reconciliation, independent duplicates, failures
before/after append, and the worker's call to the same reconciliation helper.
These tests pass with the current implementation; they do not establish the
cause of a previously stuck entry on a separate running installation.

Eight further regressions cover stream-key rotation past the handoff, permanent
handoff consumption despite retained history, queue switching, stale recording
notifications, failed/ineffective moves, and original-device/username ownership
while refreshing paused or renewed streams. The initial three reproductions
failed before the playback correction and pass afterward.

The dynamic boundary correction adds 23 backend regressions covering API/service
locks, legal future reordering, transition lock transfer, permanently consumed
handoff IDs, foreign tails and interspersed duplicate recordings, idempotence,
concurrent edit/reconcile, stale queue/stream/next observations, append boundary
drift, missing/consumed manual queue regions, duplicate PMS IDs, and upgrades of
an existing Rooms table. Six additional startup regressions and the revised two
manual-region cases cover automatic initialization, consumed regions, exact
order preservation, failure/retry, interrupted initialization, transition races,
and concurrent first requests. The PMS fake models insertion at the manual-region
end, even when that differs from the full queue tail. Three browser
regressions cover locked host controls, legal reordering directly below Up Next,
SSE lock transfer, and guest labels without queue controls.

## Real Plex acceptance checks

Unit tests mock PMS; browser tests use fixture state. Before deploying, validate
with the configured PMS and a remote/cellular Plexamp client:

1. Match different linked users and dynamically discovered devices; observe
   notifications, including disconnect/reconnect and a track boundary at startup.
2. Preserve current + next; verify later originals disappear and new songs are
   adopted when Plexamp advances. Test append, late insertion, duplicate tracks,
   reorder, and removal by queue item ID. Continue across several track/stream
   transitions: the handoff must disappear and never reappear, Now Playing must
   follow the live device, and queue management must follow its immediate next
   item. Verify the new next Room row locks on each transition. Inject foreign
   tracks at the tail and between future requests and confirm removal before
   they reach Up Next; an unexpected next item must remain protected.
3. Acquire a missing recording through Lidarr and Plex's existing scan/enrichment
   lifecycle; confirm its reserved position fills after exact readiness.
4. Interrupt PMS writes/connections and restart Melodarr; verify journal recovery,
   safe error states, server/queue switching, and dynamic current/next protection.
5. Verify SSE through the deployment's reverse proxy and HTTPS cookie settings.
6. Exercise near-empty and exhausted queues; Room must warn and leave manual
   playback/restart to the host. End Room while music continues.
