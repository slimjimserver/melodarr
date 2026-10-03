interface RoomTrack { title: string; artist: string; album?: string }
interface RoomEntry extends RoomTrack { id: string; requester?: string | null; state: string; locked?: boolean; error?: string; artwork?: string }
interface RoomState {
  code: string; status: string; version: number; joinPath: string;
  nowPlaying: RoomTrack; handoff: Partial<RoomTrack>; upNext?: Partial<RoomTrack>; queue: RoomEntry[];
  queueWarning: boolean; syncError?: string; playbackState: string; guestCount: number;
}
interface RoomChoice extends RoomTrack { id: string; state: string; artwork?: string }

const root = document.querySelector<HTMLElement>("#room-root")!;
const host = root?.dataset.host === "true";
let csrf = "", guestCsrf = "", code = "", current: RoomState | undefined;
let generation = 0, source: EventSource | undefined, reconnect: number | undefined, retryDelay = 2000;
let message!: HTMLElement, connection!: HTMLElement, roomPanel!: HTMLElement, searchPanel!: HTMLElement;

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
    node.disabled = true;
    message.textContent = "";
    try { await action(); } catch (error) { if (generation === actionGeneration) message.textContent = error.message; }
    finally { node.disabled = false; }
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
  if (!response.ok) throw new Error(result.error || "The Room could not be updated. Retry shortly.");
  return result;
}
function stop() {
  generation++;
  source?.close(); source = undefined;
  if (reconnect !== undefined) window.clearTimeout(reconnect);
  reconnect = undefined;
}
function mount() {
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
};
function trackCard(track: Partial<RoomTrack>, label: string) {
  const card = element("section", "", "room-track");
  card.append(element("p", label, "eyebrow"), element("h3", track.title || "No track detected"), element("p", track.artist || ""));
  return card;
}
function render(state: RoomState) {
  if (current?.code === state.code && current.version > state.version) return;
  current = state; code = state.code;
  roomPanel.replaceChildren();
  const heading = element("div", "", "room-heading");
  heading.append(element("h2", `Room ${state.code}`), element("p", `${state.guestCount} guests joined`));
  roomPanel.append(heading);
  if (state.status === "closed") {
    stop(); connection.textContent = "Room ended";
    roomPanel.append(element("p", "This Room has ended. Plexamp playback continues with its current queue."));
    searchPanel.hidden = true;
    if (host) roomPanel.append(button("Start another Room", () => showHostRooms(csrf)));
    return;
  }
  if (host) {
    const share = element("div", "", "room-share"), label = element("label", "Guest join URL");
    const input = element("input"); input.readOnly = true; input.value = new URL(state.joinPath, window.location.origin).href;
    label.append(input);
    share.append(label, button("Copy room URL", async () => {
      try { await navigator.clipboard.writeText(input.value); message.textContent = "Room URL copied."; }
      catch { input.focus(); input.select(); message.textContent = "Select and copy the Room URL."; }
    }));
    roomPanel.append(share);
  }
  roomPanel.append(trackCard(state.nowPlaying, state.playbackState === "playing" ? "NOW PLAYING" : `PLAYBACK ${state.playbackState.toUpperCase()}`));
  if (state.upNext?.title && !state.queue.some(entry => entry.locked)) roomPanel.append(trackCard(state.upNext, "UP NEXT · LOCKED"));
  if (host && state.queueWarning) roomPanel.append(element("p", "Room queue is almost empty. Add another playable song before playback ends.", "room-warning"));
  if (state.syncError) roomPanel.append(element("p", state.syncError, "message error"));
  roomPanel.append(element("h2", "Upcoming Room queue"));
  if (!state.queue.length) roomPanel.append(element("p", "Search below to add the first Room song."));
  const list = element("ol", "", "room-queue");
  state.queue.forEach((entry, index) => {
    const row = element("li"); row.dataset.entryId = entry.id;
    if (entry.artwork) { const image = element("img"); image.src = entry.artwork; image.alt = ""; image.loading = "lazy"; row.append(image); }
    const details = element("div", "", "room-entry-detail");
    details.append(element("strong", entry.title), element("p", entry.artist));
    if (entry.requester) details.append(element("p", `Requested by ${entry.requester}`));
    details.append(element("span", stateLabels[entry.state] || "Requested", `request-lifecycle ${entry.state}`));
    if (entry.locked) details.append(element("span", "Up Next · Locked", "request-lifecycle ready"));
    if (entry.error) details.append(element("p", entry.error, "message error"));
    row.append(details);
    if (host) {
      const controls = element("div", "", "room-entry-controls");
      const move = async (offset: number) => {
        if (entry.locked || state.queue[index+offset]?.locked) return;
        const order = state.queue.map(item => item.id);
        [order[index], order[index+offset]] = [order[index+offset], order[index]];
        await mutate("order", "PUT", { entryIds: order, version: state.version });
      };
      const up = button("↑", () => move(-1)); up.setAttribute("aria-label", `Move ${entry.title} up`); up.disabled = !!entry.locked || index === 0 || !!state.queue[index-1]?.locked;
      const down = button("↓", () => move(1)); down.setAttribute("aria-label", `Move ${entry.title} down`); down.disabled = !!entry.locked || index === state.queue.length-1 || !!state.queue[index+1]?.locked;
      const remove = button("Remove", () => { if (!entry.locked) return mutate(`entries/${entry.id}`, "DELETE", { version: state.version }); }); remove.setAttribute("aria-label", `Remove ${entry.title}`); remove.disabled = !!entry.locked;
      controls.append(up, down, remove); row.append(controls);
    }
    list.append(row);
  });
  roomPanel.append(list);
  if (host) {
    const actions = element("div", "", "room-actions");
    actions.append(button("Retry synchronization", () => mutate("sync", "POST")), button("End Room", () => mutate("end", "POST")));
    roomPanel.append(actions);
  }
  roomPanel.append(element("p", "Plexamp refreshes queue changes when playback advances. Playback controls stay in Plexamp.", "room-note"));
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
        if (choice.artwork) { const image = element("img"); image.src = choice.artwork; image.alt = ""; image.loading = "lazy"; row.append(image); }
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
    roomPanel.append(button("Start Room", async () => {
      connection.textContent = "Detecting active Plexamp playback…";
      try {
        const { room } = await call<{room: RoomState}>("/api/rooms", "POST");
        if (generation !== requestGeneration) return;
        render(room); setupSearch(); watch();
      } catch (error) {
        const response = await call<{room: RoomState | null}>("/api/rooms/active").catch(() => ({ room: null }));
        if (generation === requestGeneration && response.room) { render(response.room); setupSearch(); watch(); }
        throw error;
      } finally { connection.textContent = ""; }
    }));
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
