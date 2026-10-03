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

With no outstanding Melodarr write, synchronization makes no PMS mutations:

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

A Melodarr host reorder/remove persists logical order/tombstones and a
`write_pending` journal flag. A newly ready request uses the same flag before
materializing. These writes reload the live stream and complete queue before
each PMS command, verify current/next, session, target, anchor, and expected
queue IDs, and abort safely on a race. Appends retain `add_before` IDs until
an exact new instance is recovered. Rating keys identify an interrupted append
among new IDs; they never merge/remove ordinary queue entries.

The final PMS read must confirm the complete expected sequence before entries
are marked Ready and write intent is cleared. An ineffective move/delete or
ambiguous interrupted append is an error. A follow-up passive pass is idempotent.
Unknown items appearing during interrupted write recovery are preserved and
cause a safe synchronization error instead of being deleted.

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
The idempotent migration adds `rooms.write_pending` and `room_entries.album`.
`recording_mbid` and `requester` now allow NULL. SQLite requires a transactional
child-table rebuild to relax the old NOT NULL constraints; saved IDs, MBIDs,
requesters, guest foreign keys, playback, tombstones, append journals, and the
ordering index are retained. Legacy dirty Rooms with entries retain saved write
intent on upgrade. No origin/source column or sentinel UUID is added.

Missing songs still reserve a logical position and use the existing
`recording_requests` lifecycle: Requested → Queued → Downloading → Waiting for
Plex → Ready. The host sponsors guest acquisition through the existing request
history and notification path. Removing a Room entry never cancels shared
acquisition. The acquisition bridge continues during PMS outages. Already
materialized songs use PMS readiness without depending on acquisition-cache
availability. The frontend omits the requester line when no requester exists.
PMS artwork URLs are not exposed to guests; safe album metadata is retained,
while existing release-group artwork continues for known requests.

### Pending ordering follow-up

Pending requests retain their position before the next surviving materialized
anchor, or at the tail. Normal late readiness fills that reserved position while
it remains behind Up Next. If an external reorder changes the order of existing
anchors while placeholders exist, synchronization preserves pending intent and
reports an error without guessing a merged order or writing PMS. Likewise, a
pending position ahead of a locked materialized entry cannot be filled ahead
of it. Playback advancement may resolve the conflict; otherwise end the Room
and start another. Comprehensive ordering for simultaneous pending downloads
and external reorders remains a separate iteration. No acquisition state machine
is added by this change.

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
| Targeted backend: `python -m unittest tests.test_rooms` | 98 passed |
| Full backend: `python -m unittest discover -s tests -t . -v` | 1,048 passed |
| Targeted browser: `pnpm run test:browser tests/rooms.spec.ts` | 13 passed |
| Full browser: `pnpm run test:browser` | 197 passed |
| Frontend: `pnpm run check`, `pnpm run build` | Passed |
| Ruff on Rooms modules/tests; repository `E9,F63,F7,F82`; changed Python formatting | Passed |
| `git diff --check` | Passed |

Coverage replaces suffix-deletion expectations with adoption and adds 16 backend
and three browser regressions for startup imports, external edits, duplicates,
metadata, pending preservation, SSE/idempotence, write races, schema/journal
upgrades, and host editing beyond the guest request limit. Existing acquisition,
security, and concurrency regressions remain in the passing suites.

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
   boundaries, and safe failure on ambiguous external changes during recovery.
   Acquire a missing recording through Lidarr/Plex and verify late readiness and
   the documented pending-order conflict.
6. Verify SSE add/remove/reorder through the HTTPS proxy, scoped guest cookies,
   near-empty warnings, and exhausted-player behavior.
