interface RoomTrack { title: string; artist: string; album?: string; artwork?: string; artworkFallback?: string }
interface RoomEntry extends RoomTrack { id: string; recordingMbid?: string | null; requester?: string | null; state: string; locked?: boolean; error?: string }
interface RoomState {
  code: string; status: string; version: number; joinPath: string;
  nowPlaying: RoomTrack; handoff: Partial<RoomTrack>; upNext?: Partial<RoomTrack>; queue: RoomEntry[];
  queueWarning: boolean; syncError?: string; playbackState: string; guestCount: number;
}
interface RoomChoice extends RoomTrack { id: string; state: string; artwork?: string }
interface RoomSession extends RoomTrack { id: string; deviceName: string; product: string; platform: string; state: string }
class RoomCallError extends Error {
  selectionRequired = false;
  sessions: RoomSession[] = [];
}

const root = document.querySelector<HTMLElement>("#room-root")!;
const host = root?.dataset.host === "true";
let csrf = "", guestCsrf = "", code = "", current: RoomState | undefined;
let generation = 0, source: EventSource | undefined, reconnect: number | undefined, retryDelay = 2000;
let message!: HTMLElement, connection!: HTMLElement, roomPanel!: HTMLElement, searchPanel!: HTMLElement;
let player: ReturnType<typeof createPlayer> | undefined;
const queueRows = new Map<string, ReturnType<typeof createQueueRow>>();

function element<K extends keyof HTMLElementTagNameMap>(tag: K, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}
function button(text: string, action: () => Promise<void> | void) {
  const node = element("button", text);
  node.type = "button";
  node.addEventListener("click", async () => {
    const actionGeneration = generation;
    node.dataset.busy = "true";
    node.disabled = true;
    message.textContent = "";
    try { await action(); } catch (error) { if (generation === actionGeneration) message.textContent = error.message; }
    finally { delete node.dataset.busy; node.disabled = node.dataset.unavailable === "true"; }
  });
  return node;
}
async function call<T>(url: string, method = "GET", payload?: unknown): Promise<T> {
  const headers = new Headers({ "X-Room-Request": "1" });
  if (payload !== undefined) headers.set("Content-Type", "application/json");
  if (host && csrf) headers.set("X-CSRF-Token", csrf);
  if (!host && guestCsrf) headers.set("X-Room-CSRF", guestCsrf);
  const response = await fetch(url, { method, headers, body: payload === undefined ? undefined : JSON.stringify(payload) });
  const result = await response.json().catch(() => { throw new Error("The Room returned an unexpected response. Retry shortly."); });
  if (!response.ok) {
    const error = new RoomCallError(result.error || "The Room could not be updated. Retry shortly.");
    error.selectionRequired = result.selectionRequired === true;
    error.sessions = Array.isArray(result.sessions) ? result.sessions : [];
    throw error;
  }
  return result;
}
function stop() {
  generation++;
  source?.close(); source = undefined;
  if (reconnect !== undefined) window.clearTimeout(reconnect);
  reconnect = undefined;
}
function mount() {
  player = undefined; queueRows.clear();
  root.replaceChildren();
  message = element("p", "", "message error"); message.setAttribute("role", "status");
  connection = element("p", "", "room-connection");
  roomPanel = element("div", "", "room-panel");
  searchPanel = element("section", "", "room-search");
  root.append(message, connection, roomPanel, searchPanel);
}
async function reload() {
  const requestGeneration = generation;
  if (code) {
    const response = await call<{room: RoomState}>(`/api/rooms/${code}`);
    if (generation === requestGeneration) render(response.room);
  }
}
async function mutate(path: string, method: string, payload?: unknown) {
  const requestGeneration = generation;
  try {
    const response = await call<{room: RoomState}>(`/api/rooms/${code}/${path}`, method, payload);
    if (generation === requestGeneration) render(response.room);
  } catch (error) { if (generation === requestGeneration) await reload().catch(() => {}); throw error; }
}
function watch() {
  source?.close();
  if (current?.status === "closed") return;
  const thisGeneration = generation;
  source = new EventSource(`/api/rooms/${code}/events`);
  source.addEventListener("room", (event) => {
    if (generation !== thisGeneration) return;
    retryDelay = 2000;
    connection.textContent = "Room updates connected";
    render(JSON.parse((event as MessageEvent).data));
  });
  source.onerror = () => {
    source?.close();
    if (generation !== thisGeneration || current?.status === "closed") return;
    connection.textContent = "Reconnecting to Room updates…";
    reconnect = window.setTimeout(() => { if (generation === thisGeneration) watch(); }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 30000);
  };
}
const stateLabels: Record<string, string> = {
  ready: "Ready", requested: "Requested", not_requested: "Requested", queued: "Queued",
  downloading: "Downloading", waiting_for_plex: "Waiting for Plex",
  waiting_for_queue: "Ready · Waiting for queue",
};
function setText(node: HTMLElement, text: string) {
  if (node.textContent !== text) node.textContent = text;
}
function setDisabled(node: HTMLButtonElement, disabled: boolean) {
  node.dataset.unavailable = String(disabled);
  node.disabled = disabled || node.dataset.busy === "true";
}
function createArtwork(eager = false) {
  const frame = element("div", "", "room-artwork");
  const placeholder = element("span", "♫", "room-artwork-placeholder"); placeholder.setAttribute("aria-hidden", "true");
  const image = element("img"); image.width = eager ? 640 : 64; image.height = eager ? 640 : 64;
  image.loading = eager ? "eager" : "lazy"; image.decoding = "async"; image.hidden = true;
  if (eager) image.setAttribute("fetchpriority", "high");
  frame.append(placeholder, image);
  let identity = "", fallback = "";
  const sized = (url: string) => `${url}${url.includes("?") ? "&" : "?"}size=${eager ? "large" : "thumb"}`;
  image.addEventListener("load", () => {
    if (!image.getAttribute("src") || !image.complete || !image.naturalWidth) return;
    image.hidden = false; frame.classList.add("has-artwork"); frame.removeAttribute("role"); frame.removeAttribute("aria-label");
  });
  image.addEventListener("error", () => {
    if (fallback) { const url = fallback; fallback = ""; image.src = sized(url); }
    else { image.hidden = true; frame.classList.remove("has-artwork"); frame.setAttribute("role", "img"); frame.setAttribute("aria-label", "Album artwork unavailable"); }
  });
  return { frame, update(track: Partial<RoomTrack>) {
    image.alt = `Album artwork for ${track.album || track.title || "the current track"}${track.artist ? ` by ${track.artist}` : ""}`;
    const nextIdentity = `${track.artwork || ""}|${track.artworkFallback || ""}`;
    if (identity === nextIdentity) return;
    identity = nextIdentity; fallback = track.artworkFallback || "";
    image.hidden = true; frame.classList.remove("has-artwork");
    frame.setAttribute("role", "img"); frame.setAttribute("aria-label", "Album artwork unavailable");
    if (track.artwork) { image.src = sized(track.artwork); image.hidden = false; }
    else image.removeAttribute("src");
  } };
}
function createPlayer() {
  const heading = element("div", "", "room-heading");
  const roomInfo = element("div"), name = element("h2"), guests = element("p"); roomInfo.append(name, guests); heading.append(roomInfo);
  if (host) {
    const disclosure = element("details", "", "room-invite"), input = element("input"), label = element("label", "Guest join URL");
    input.readOnly = true; label.append(input); disclosure.append(element("summary", "Invite link"), label); roomInfo.append(disclosure);
    const invite = button("Copy Invite Link", async () => {
      const url = new URL(current!.joinPath, window.location.origin).href;
      try { await navigator.clipboard.writeText(url); message.textContent = "Invite link copied."; }
      catch {
        disclosure.open = true; input.focus(); input.select(); message.textContent = "Select and copy the invite link.";
      }
    });
    heading.append(invite);
  }
  const hero = element("section", "", "room-now-playing"); hero.setAttribute("aria-label", "Now Playing");
  const artwork = createArtwork(true), metadata = element("div", "", "room-now-metadata");
  const label = element("p", "NOW PLAYING", "eyebrow"), title = element("h2", "", "room-now-title");
  const artist = element("p", "", "room-now-artist"), album = element("p", "", "room-now-album");
  metadata.append(label, title, artist, album); hero.append(artwork.frame, metadata);
  const warning = element("p", "Room queue is almost empty. Add another playable song before playback ends.", "room-warning");
  const syncError = element("p");
  const queueHeading = element("div", "", "room-queue-heading"), count = element("span");
  queueHeading.append(element("h2", "Up Next"), count);
  const next = element("div", "", "room-next-preview"), nextArtwork = createArtwork();
  const nextDetail = element("div", "", "room-entry-detail"), nextTitle = element("strong"), nextArtist = element("p");
  nextDetail.append(nextTitle, nextArtist, element("span", "Up Next · Locked", "room-lock")); next.append(nextArtwork.frame, nextDetail);
  const empty = element("p", "Search below to add the first Room song.", "room-empty");
  const list = element("ol", "", "room-queue"); list.setAttribute("aria-label", "Up Next queue");
  roomPanel.replaceChildren(heading, hero, warning, syncError, queueHeading, next, empty, list);
  if (host) {
    const actions = element("div", "", "room-actions");
    actions.append(button("Retry synchronization", () => mutate("sync", "POST")), button("End Room", () => mutate("end", "POST")));
    roomPanel.append(actions);
  }
  roomPanel.append(element("p", "Playback stays in Plexamp. Queue changes refresh there when playback advances.", "room-note"));
  return { name, guests, artwork, label, title, artist, album, warning, syncError, count, next, nextArtwork, nextTitle, nextArtist, empty, list };
}
function createQueueRow(id: string) {
  const row = element("li"); row.dataset.entryId = id;
  const artwork = createArtwork(), details = element("div", "", "room-entry-detail");
  const title = element("strong", "", "room-entry-title"), artist = element("p", "", "room-entry-artist");
  const meta = element("div", "", "room-entry-meta"), requester = element("span", "", "room-requester");
  const status = element("span", "", "request-lifecycle"), lock = element("span", "Up Next · Locked", "room-lock");
  const error = element("p"); meta.append(lock, status, requester); details.append(title, artist, meta, error);
  row.append(artwork.frame, details);
  let controls: { up: HTMLButtonElement; down: HTMLButtonElement; remove: HTMLButtonElement } | undefined;
  if (host) {
    const group = element("div", "", "room-entry-controls"); group.setAttribute("role", "group"); group.setAttribute("aria-label", "Queue actions");
    const move = async (offset: number) => {
      const state = current!, index = state.queue.findIndex(entry => entry.id === id);
      if (index < 0 || state.queue[index].locked || !state.queue[index+offset] || state.queue[index+offset].locked) return;
      const order = state.queue.map(entry => entry.id);
      [order[index], order[index+offset]] = [order[index+offset], order[index]];
      await mutate("order", "PUT", { entryIds: order, version: state.version });
    };
    const up = button("↑", () => move(-1)), down = button("↓", () => move(1));
    const remove = button("×", () => {
      if (current?.queue.some(entry => entry.id === id && !entry.locked)) return mutate(`entries/${id}`, "DELETE", { version: current.version });
    });
    controls = { up, down, remove }; group.append(up, down, remove); row.append(group);
  }
  return { row, update(entry: RoomEntry, index: number, state: RoomState) {
    artwork.update(entry); setText(title, entry.title); setText(artist, entry.artist);
    setText(requester, entry.requester ? `Requested by ${entry.requester}` : ""); requester.hidden = !entry.requester;
    // Guest presentation is limited even if an intermediate host state arrives.
    const lifecycle = host ? entry.state : entry.state === "ready" ? "ready" : "requested";
    setText(status, stateLabels[lifecycle] || "Requested"); status.className = `request-lifecycle ${lifecycle}`;
    lock.hidden = !entry.locked;
    setText(error, host ? entry.error || "" : ""); error.hidden = !host || !entry.error;
    error.className = host && entry.error ? "message error" : "";
    if (controls) {
      controls.up.setAttribute("aria-label", `Move ${entry.title} up`); controls.up.title = "Move up";
      controls.down.setAttribute("aria-label", `Move ${entry.title} down`); controls.down.title = "Move down";
      controls.remove.setAttribute("aria-label", `Remove ${entry.title}`); controls.remove.title = "Remove from queue";
      setDisabled(controls.up, !!entry.locked || index === 0 || !!state.queue[index-1]?.locked);
      setDisabled(controls.down, !!entry.locked || index === state.queue.length-1 || !!state.queue[index+1]?.locked);
      setDisabled(controls.remove, !!entry.locked);
    }
  } };
}
function render(state: RoomState) {
  if (current?.code === state.code && current.version > state.version) return;
  current = state; code = state.code;
  if (state.status === "closed") {
    stop(); connection.textContent = "Room ended";
    player = undefined; queueRows.clear();
    roomPanel.replaceChildren(element("h2", `Room ${state.code}`), element("p", "This Room has ended. Plexamp playback continues with its current queue."));
    searchPanel.hidden = true;
    if (host) roomPanel.append(button("Start another Room", () => showHostRooms(csrf)));
    return;
  }
  player ||= createPlayer();
  setText(player.name, `Room ${state.code}`); setText(player.guests, `${state.guestCount} ${state.guestCount === 1 ? "guest" : "guests"}`);
  const inviteInput = roomPanel.querySelector<HTMLInputElement>(".room-invite input");
  if (inviteInput) inviteInput.value = new URL(state.joinPath, window.location.origin).href;
  player.artwork.update(state.nowPlaying);
  setText(player.label, state.playbackState === "playing" ? "NOW PLAYING" : `PLAYBACK ${state.playbackState.toUpperCase()}`);
  setText(player.title, state.nowPlaying.title || "No track detected");
  setText(player.artist, state.nowPlaying.artist || ""); setText(player.album, state.nowPlaying.album || "");
  player.warning.hidden = !host || !state.queueWarning;
  setText(player.syncError, state.syncError || ""); player.syncError.hidden = !state.syncError;
  player.syncError.className = state.syncError ? "message error" : "";
  const showNext = !!state.upNext?.title && !state.queue.some(entry => entry.locked);
  player.next.hidden = !showNext;
  if (showNext) { player.nextArtwork.update(state.upNext!); setText(player.nextTitle, state.upNext!.title!); setText(player.nextArtist, state.upNext!.artist || ""); }
  setText(player.count, `${state.queue.length + Number(showNext)} tracks`);
  player.empty.hidden = state.queue.length > 0 || showNext;
  const ids = new Set(state.queue.map(entry => entry.id));
  for (const [id, item] of queueRows) { if (!ids.has(id)) { item.row.remove(); queueRows.delete(id); } }
  state.queue.forEach((entry, index) => {
    let item = queueRows.get(entry.id);
    if (!item) { item = createQueueRow(entry.id); queueRows.set(entry.id, item); }
    item.update(entry, index, state);
    if (player!.list.children[index] !== item.row) player!.list.insertBefore(item.row, player!.list.children[index] || null);
  });
  searchPanel.hidden = false;
}
function setupSearch() {
  searchPanel.replaceChildren(element("h2", "Find a track"));
  const form = element("form", "", "room-search-form"), label = element("label", "Track or artist");
  const input = element("input"); input.type = "search"; input.required = true; input.minLength = 2; input.maxLength = 200; label.append(input);
  const submit = element("button", "Search"); submit.type = "submit"; form.append(label, submit);
  const results = element("div", "", "room-search-results"); searchPanel.append(form, results);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); submit.disabled = true; message.textContent = "";
    const requestGeneration = generation; results.setAttribute("aria-busy", "true");
    try {
      const response = await call<{results: RoomChoice[]}>(`/api/rooms/${code}/search?q=${encodeURIComponent(input.value)}`);
      if (generation !== requestGeneration) return;
      results.replaceChildren();
      if (!response.results.length) results.append(element("p", "No requestable tracks found. Try a more specific title and artist."));
      response.results.forEach((choice) => {
        const row = element("div", "", "room-search-result");
        const artwork = createArtwork(); artwork.update(choice); row.append(artwork.frame);
        const detail = element("div", "", "room-entry-detail");
        detail.append(element("strong", choice.title), element("p", choice.artist), element("p", choice.album || ""), element("span", stateLabels[choice.state] || "Request to acquire"));
        const add = button("Request track", async () => {
          const requestGeneration = generation;
          await mutate("entries", "POST", { choiceId: choice.id });
          if (generation === requestGeneration) message.textContent = `${choice.title} added to the Room.`;
        });
        add.setAttribute("aria-label", `Request ${choice.title}`); row.append(detail, add); results.append(row);
      });
    } catch (error) { if (generation === requestGeneration) message.textContent = error.message; }
    finally { submit.disabled = false; results.removeAttribute("aria-busy"); }
  });
}
export async function showHostRooms(token: string) {
  stop(); csrf = token; code = ""; current = undefined; mount();
  const requestGeneration = generation;
  try {
    const { room } = await call<{room: RoomState | null}>("/api/rooms/active");
    if (generation !== requestGeneration) return;
    if (room) { render(room); setupSearch(); watch(); return; }
    roomPanel.append(element("p", "First start playing music in Plexamp and add at least one more song to Up Next. Your existing Plex queue is imported into the Room; current and Up Next stay protected.", "intro"));
    const startRoom = async (sessionId?: string) => {
      connection.textContent = "Detecting active Plexamp playback…";
      try {
        const { room } = await call<{room: RoomState}>("/api/rooms", "POST", sessionId ? { sessionId } : undefined);
        if (generation !== requestGeneration) return;
        render(room); setupSearch(); watch();
      } catch (error) {
        if (generation !== requestGeneration) return;
        if (error instanceof RoomCallError && error.selectionRequired) {
          showDevices(error.sessions); message.textContent = error.message; return;
        }
        const response = await call<{room: RoomState | null}>("/api/rooms/active").catch(() => ({ room: null }));
        if (generation === requestGeneration && response.room) { render(response.room); setupSearch(); watch(); }
        throw error;
      } finally { connection.textContent = ""; }
    };
    const showDevices = (sessions: RoomSession[]) => {
      roomPanel.querySelector(".room-device-picker")?.remove();
      const picker = element("section", "", "room-device-picker");
      const form = element("form"), devices = element("fieldset");
      devices.append(element("legend", "Choose a Plexamp device"));
      sessions.forEach((session) => {
        const label = element("label"), radio = element("input");
        radio.type = "radio"; radio.name = "sessionId"; radio.value = session.id; radio.required = true;
        const details = element("span");
        details.append(element("strong", session.deviceName || session.product || "Plexamp device"),
          element("p", [session.product, session.platform].filter(Boolean).join(" · ")),
          element("p", `${session.state === "paused" ? "Paused" : "Playing"}: ${session.title}${session.artist ? ` — ${session.artist}` : ""}`));
        label.append(radio, details); devices.append(label);
      });
      const submit = element("button", "Start Room on selected device"); submit.type = "submit"; submit.disabled = sessions.length === 0;
      form.append(devices, submit);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); submit.disabled = true; message.textContent = "";
        const selection = new FormData(form).get("sessionId");
        try { if (typeof selection === "string") await startRoom(selection); }
        catch (error) { if (generation === requestGeneration) message.textContent = error.message; }
        finally { submit.disabled = sessions.length === 0; }
      });
      picker.append(form, button("Refresh devices", async () => {
        const response = await call<{sessions: RoomSession[]}>("/api/rooms/sessions");
        if (generation === requestGeneration) showDevices(response.sessions);
      }));
      if (!sessions.length) picker.append(element("p", "No active Plexamp devices found. Start playback, then refresh devices."));
      roomPanel.append(picker);
    };
    roomPanel.append(button("Start Room", () => startRoom()));
  } catch (error) { if (generation === requestGeneration) message.textContent = error.message; }
}
window.addEventListener("melodarr-view-changed", (event) => { if ((event as CustomEvent).detail !== "rooms") stop(); });
window.addEventListener("melodarr-signed-out", stop);
window.addEventListener("pagehide", stop);
if (root?.dataset.guest === "true") {
  mount(); code = window.location.pathname.split("/")[2]?.toUpperCase() || "";
  const form = element("form", "", "room-join"), label = element("label", "Your name (optional)");
  const name = element("input"); name.maxLength = 60; name.setAttribute("autocomplete", "nickname"); label.append(name);
  const submit = element("button", "Join Room"); submit.type = "submit"; form.append(label, submit); roomPanel.append(form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); submit.disabled = true; message.textContent = "";
    try {
      const response = await call<{room: RoomState; guest: {name: string; csrfToken: string}}>(`/api/rooms/${code}/join`, "POST", { name: name.value });
      guestCsrf = response.guest.csrfToken; render(response.room); setupSearch(); watch(); message.textContent = `Joined as ${response.guest.name}`;
    } catch (error) { message.textContent = error.message; submit.disabled = false; }
  });
}
