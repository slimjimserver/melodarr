type JsonObject = Record<string, any>;
type AppForm = HTMLFormElement & {
  username: HTMLInputElement;
  password: HTMLInputElement;
  remember: HTMLInputElement;
  listenbrainzUsername: HTMLInputElement;
  lastfmUsername: HTMLInputElement;
  lastfmApiKey: HTMLInputElement;
  invitationLink: HTMLInputElement;
};
interface AppElement extends HTMLElement {
  value: string;
  placeholder: string;
  checked: boolean;
  disabled: boolean;
  open: boolean;
  selectedOptions: HTMLCollectionOf<HTMLOptionElement>;
  hostname: HTMLInputElement;
  port: HTMLInputElement;
  useSsl: HTMLInputElement;
  externalUrl: HTMLInputElement;
  monitor: HTMLSelectElement;
  monitorNewItems: HTMLSelectElement;
  searchForMissingAlbums: HTMLInputElement;
  remember: HTMLInputElement;
  reset(): void;
  close(): void;
  showModal(): void;
  src: string;
  alt: string;
  fetchPriority: string;
}
type AccountPage = "profile" | "requests" | "general" | "linked-accounts" | "invitations";
type AppView = "discover" | "detail" | "library" | "settings" | "account";
type SettingsPage = "services" | "requests" | "users" | "jobs";
type ThemeName = "midnight" | "warm";

interface CurrentUser {
  id?: number;
  username: string;
  role: "admin" | "user";
  csrfToken?: string;
  authProvider?: "local" | "plex";
  plexLinked?: boolean;
  plexUsername?: string;
  plexEmail?: string;
  listenbrainzUsername?: string;
  lastfmUsername?: string;
  lastfmConfigured?: boolean;
}

interface AdminUserIdentity {
  id: number;
  username: string;
  localUsername?: string;
  userType: "plex" | "local";
  role: "admin" | "user";
  plexUsername?: string;
  plexEmail?: string;
  plexAvatar?: string;
}

interface AdminUser extends AdminUserIdentity {
  requestCount: number;
  joinedAt: number | string;
  listenbrainzUsername?: string;
  lastfmUsername?: string;
  lastfmConfigured?: boolean;
}

interface AdminRequest {
  id: number;
  kind: "artist" | "release-group";
  mbid: string;
  name: string;
  artist_name?: string;
  release_type?: string;
  release_date?: string;
  created_at: number | string;
  availableInPlex: boolean;
  plexUrl?: string;
  plexampUrl?: string;
  requester: AdminUserIdentity;
}

interface AdminRequestPagination {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}

type AdminUserForm = HTMLFormElement & {
  role: HTMLSelectElement;
  localUsername: HTMLInputElement;
  password: HTMLInputElement;
  listenbrainzUsername: HTMLInputElement;
  lastfmUsername: HTMLInputElement;
  lastfmApiKey: HTMLInputElement;
};

interface PlexConnection extends JsonObject {
  uri: string;
  local: boolean;
  secure: boolean;
}

interface PlexServer extends JsonObject {
  id: string;
  name: string;
  connections: PlexConnection[];
}

interface LidarrDefaults extends JsonObject {
  rootFolderPath?: string;
  qualityProfileId?: number;
  metadataProfileId?: number;
  tags?: number[];
  monitor?: string;
  monitorNewItems?: string;
  searchForMissingAlbums?: boolean;
}

function $<T extends Element = AppElement>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required element not found: ${selector}`);
  return element;
}

function requiredDescendant<T extends Element>(parent: ParentNode, selector: string): T {
  const element = parent.querySelector<T>(selector);
  if (!element) throw new Error(`Required descendant not found: ${selector}`);
  return element;
}

function normalizeSearch(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .trim();
}

function isMobileDevice() {
  return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function mobilePlexDestination(plexUrl: string, plexampUrl: string) {
  return isMobileDevice() && plexampUrl
    ? { url: plexampUrl, label: "Open in Plexamp", openInNewTab: false }
    : { url: plexUrl, label: "Open in Plex", openInNewTab: true };
}

let lidarrOptions: JsonObject | undefined;
let lidarrDefaults: LidarrDefaults = {};
let currentUser: CurrentUser | undefined;
let showAccountPage: ((
  page?: AccountPage,
  updateHistory?: boolean,
  username?: string,
  requestPage?: number,
) => void) | undefined;
let invitationToken = "";
let setupPlexFlowToken = "";
let setupPlexServers: PlexServer[] = [];
let settingsPlexFlowToken = "";
let settingsPlexServers: PlexServer[] = [];
let maintenanceRefreshTimer: number | undefined;
let maintenanceRefreshInFlight = false;
let adminUsers: AdminUser[] = [];
let editingAdminUser: AdminUser | undefined;
let adminUsersRequest = 0;
let adminRequests: AdminRequest[] = [];
let adminRequestsRequest = 0;
let adminRequestsPagination: AdminRequestPagination = {
  page: 1,
  pageSize: 100,
  total: 0,
  totalPages: 0,
};
let discoveryLoad: Promise<void> | undefined;
// Plex holdings tell a requester what is already available, so the library is
// readable by every account. Settings remains administrator-only.
const VIEWS_FOR_EVERY_USER = ["discover", "detail", "library", "account"];

if ("scrollRestoration" in window.history) {
  window.history.scrollRestoration = "manual";
}

function resetPageScroll() {
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
}

/**
 * Restore pull-to-refresh for installed PWAs, where the browser's own gesture
 * is not consistently exposed. It only activates from the document top so it
 * cannot interfere with normal scrolling or horizontal carousels.
 */
function setupStandalonePullToRefresh() {
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches
    || (navigator as Navigator & { standalone?: boolean }).standalone === true;
  if (!isStandalone) return;

  const indicator = document.querySelector<HTMLElement>("#pull-to-refresh");
  if (!indicator) return;
  let startY = 0;
  let tracking = false;
  const threshold = 84;

  document.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1 || window.scrollY > 0) return;
    const target = event.target as Element | null;
    if (target?.closest("input, textarea, select, button, a, [contenteditable=true]")) return;
    startY = event.touches[0].clientY;
    tracking = true;
  }, { passive: true });

  document.addEventListener("touchmove", (event) => {
    if (!tracking) return;
    const distance = Math.max(0, event.touches[0].clientY - startY);
    if (!distance) return;
    if (distance > 8) event.preventDefault();
    const progress = Math.min(distance / threshold, 1);
    indicator.style.setProperty("--pull-progress", String(progress));
    indicator.classList.toggle("ready", distance >= threshold);
    indicator.classList.add("visible");
  }, { passive: false });

  document.addEventListener("touchend", () => {
    if (!tracking) return;
    const shouldRefresh = indicator.classList.contains("ready");
    tracking = false;
    indicator.classList.remove("ready", "visible");
    indicator.style.removeProperty("--pull-progress");
    if (shouldRefresh) window.location.reload();
  }, { passive: true });
}

function setMessage(element: Element, message: string, isError = false) {
  element.textContent = message;
  element.classList.add("message");
  element.classList.toggle("error", isError);
}

function setupTheme() {
  const button = $<HTMLButtonElement>("#theme-toggle");
  const icon = requiredDescendant<HTMLElement>(button, ".theme-icon");
  const label = requiredDescendant<HTMLElement>(button, ".theme-label");
  const storageKey = "melodarr-theme";

  const applyTheme = (theme: ThemeName, persist = false) => {
    const nextTheme: ThemeName = theme === "midnight" ? "warm" : "midnight";
    document.documentElement.dataset.theme = theme;
    icon.textContent = theme === "midnight" ? "☾" : "☀";
    label.textContent = theme === "midnight" ? "Midnight" : "Warm";
    button.setAttribute("aria-label", `Switch to ${nextTheme === "midnight" ? "Midnight" : "Warm"} theme`);
    button.title = `Switch to ${nextTheme === "midnight" ? "Midnight" : "Warm"} theme`;
    document.querySelector('meta[name="theme-color"]')?.setAttribute(
      "content",
      theme === "midnight" ? "#050506" : "#f6f0e7",
    );
    if (persist) {
      try {
        window.localStorage.setItem(storageKey, theme);
      } catch {
        // The selected theme still applies for this page when storage is
        // unavailable in a strict privacy mode.
      }
    }
  };

  applyTheme(document.documentElement.dataset.theme === "warm" ? "warm" : "midnight");
  button.addEventListener("click", () => {
    applyTheme(
      document.documentElement.dataset.theme === "midnight" ? "warm" : "midnight",
      true,
    );
  });
}

function loadDiscovery() {
  if (discoveryLoad) return discoveryLoad;
  discoveryLoad = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = document.body.dataset.discoverySrc || "/static/discovery.js";
    script.async = true;
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => {
      discoveryLoad = undefined;
      reject(new Error("We couldn’t finish loading the music browser. Please refresh and try again."));
    }, { once: true });
    document.body.append(script);
  });
  return discoveryLoad;
}

async function copyInputValue(input: HTMLInputElement) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(input.value);
      return true;
    } catch {
      // Fall through to the synchronous copy path for denied permissions.
    }
  }
  try {
    input.select();
    input.setSelectionRange(0, input.value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  }
}

/**
 * Announce the result of an action next to the reader's thumb.
 *
 * Request buttons can sit hundreds of rows down a discography, where a message
 * written into the page heading is never seen.
 */
function showToast(message: string, isError = false) {
  if (!message) return;
  const container = document.querySelector("#toasts");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " error" : ""}`;
  toast.setAttribute("role", isError ? "alert" : "status");
  toast.textContent = message;
  container.append(toast);
  window.setTimeout(() => {
    toast.classList.add("leaving");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, isError ? 7_000 : 4_500);
}

/** Build a shimmering stand-in with the same shape as the pending content. */
function skeletonBlock(className: string, count = 1) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) {
    const block = document.createElement("div");
    block.className = `skeleton ${className}`;
    fragment.append(block);
  }
  return fragment;
}

async function api<T = JsonObject>(url: string, options: RequestInit = {}): Promise<T> {
  const requestOptions = { ...options };
  const method = (requestOptions.method || "GET").toUpperCase();
  const headers = new Headers(requestOptions.headers || {});
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && currentUser?.csrfToken) {
    headers.set("X-CSRF-Token", currentUser.csrfToken);
  }
  requestOptions.headers = headers;
  const response = await fetch(url, requestOptions);
  const responseText = await response.text();
  let body: T & { error?: string };
  try {
    body = (responseText ? JSON.parse(responseText) : {}) as T & { error?: string };
  } catch {
    const message = response.ok
      ? "We received an unexpected response. Please try again."
      : "We couldn’t complete that request. Please try again.";
    throw new Error(message);
  }

  if (!response.ok) {
    throw new Error(body.error || "We couldn’t complete that request. Please try again.");
  }
  return body;
}

function wait(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

async function startPlexAuthentication(
  purpose: "login" | "server" | "link",
  message: Element,
  onComplete: (result: JsonObject) => Promise<void> | void,
) {
  const popup = window.open(
    "",
    "Melodarr Plex Sign In",
    "popup=yes,scrollbars=yes,width=600,height=700",
  );
  if (!popup) {
    setMessage(message, "Allow popups for Melodarr, then try again.", true);
    return;
  }
  popup.document.title = "Opening Plex…";
  popup.document.body.textContent = "Opening secure Plex sign-in…";
  setMessage(message, "Opening secure Plex sign-in…");

  try {
    const started = await api("/api/auth/plex/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose }),
    });
    popup.location.href = started.authorizationUrl;
    const deadline = Math.min(
      Number(started.expiresAt || (Date.now() / 1000) + 900) * 1000,
      Date.now() + 15 * 60 * 1000,
    );

    while (Date.now() < deadline) {
      if (popup.closed) {
        throw new Error("Plex sign-in was closed. Try again.");
      }
      const result = await api("/api/auth/plex/poll", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flowToken: started.flowToken }),
      });
      if (!result.pending) {
        popup.close();
        await onComplete({ ...result, flowToken: started.flowToken });
        return;
      }
      await wait(1_000);
    }
    throw new Error("Plex sign-in expired before it was completed.");
  } catch (error) {
    popup.close();
    setMessage(message, error.message, true);
  }
}

function plexControlElements(scope: "setup" | "settings") {
  return {
    server: $<HTMLSelectElement>(`#${scope}-plex-server-select`),
    connection: $<HTMLSelectElement>(`#${scope}-plex-connection-select`),
  };
}

function populatePlexConnections(scope: "setup" | "settings", servers: PlexServer[]) {
  const controls = plexControlElements(scope);
  const server = servers.find((item) => item.id === controls.server.value) || servers[0];
  controls.connection.replaceChildren();
  (server?.connections || []).forEach((connection) => {
    const location = connection.local ? "local" : "remote";
    const security = connection.secure ? " · secure" : "";
    controls.connection.add(
      new Option(`${connection.uri} · ${location}${security}`, connection.uri),
    );
  });
  controls.connection.disabled = !server?.connections.length;
}

function populatePlexServers(scope: "setup" | "settings", servers: PlexServer[]) {
  const controls = plexControlElements(scope);
  controls.server.replaceChildren();
  servers.forEach((server) => controls.server.add(new Option(server.name, server.id)));
  controls.server.disabled = servers.length === 0;
  populatePlexConnections(scope, servers);
}

function selectedPlexConnection(scope: "setup" | "settings") {
  const controls = plexControlElements(scope);
  return {
    serverId: controls.server.value,
    connectionUri: controls.connection.value,
  };
}

function addSelectOptions(select: HTMLSelectElement, options: JsonObject[], labelKey: string, valueKey: string, selected: Array<string | number | undefined> = []) {
  const selectedValues = new Set(selected.map(String));
  select.replaceChildren();

  options.forEach((option) => {
    const label = option[labelKey] || option.path;
    const value = option[valueKey];
    select.add(new Option(label, value, false, selectedValues.has(String(value))));
  });
}

function populateOptionPicker(
  picker: HTMLElement,
  options: JsonObject[],
  labelKey: string,
  valueKey: string,
  selected: Array<string | number | undefined> = [],
) {
  const selectedValues = new Set(selected.map(String));
  picker.replaceChildren();

  options.forEach((option) => {
    const value = String(option[valueKey]);
    const choice = document.createElement("label");
    choice.className = "option-choice";

    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    input.checked = selectedValues.has(value);

    const label = document.createElement("span");
    label.textContent = String(option[labelKey] || option.path || value);
    choice.append(input, label);
    picker.append(choice);
  });
}

function selectedOptionPickerValues(selector: string) {
  return [...document.querySelectorAll<HTMLInputElement>(`${selector} input:checked`)]
    .map((input) => input.value);
}

function populateLidarrOptions(options: JsonObject) {
  lidarrOptions = options;
  addSelectOptions($("#default-root-folders"), options.rootFolders, "path", "path", [lidarrDefaults.rootFolderPath]);
  addSelectOptions($("#default-quality-profiles"), options.qualityProfiles, "name", "id", [lidarrDefaults.qualityProfileId]);
  addSelectOptions($("#default-metadata-profiles"), options.metadataProfiles, "name", "id", [lidarrDefaults.metadataProfileId]);
  populateOptionPicker($("#default-tags"), options.tags, "label", "id", lidarrDefaults.tags || []);
}

function populatePlexLibraries(libraries: JsonObject[], selected: Array<string | number> = []) {
  populateOptionPicker($("#plex-library-sections"), libraries, "title", "id", selected);
  $("#plex-libraries").disabled = libraries.length === 0;
}

function parseLidarrUrl(value: string) {
  try {
    const url = new URL(value);
    return { hostname: url.hostname, port: url.port || "8686", useSsl: url.protocol === "https:" };
  } catch {
    return { hostname: "", port: "8686", useSsl: false };
  }
}

async function refreshSettings(loadLidarrOptions = true) {
  const settings = await api("/api/settings");
  const { lidarr, plex } = settings;
  lidarrDefaults = lidarr.defaults || {};

  $("#lidarr-state").textContent = lidarr.configured ? `Connected · ${lidarr.url}` : "Not connected";
  $("#plex-state").textContent = plex.configured
    ? `Connected · ${plex.serverName || plex.url}`
    : "Not connected";
  $("#connect-plex").firstChild!.textContent = plex.configured
    ? "Reconnect with "
    : "Sign in with ";
  const currentPlexConnection = $("#plex-current-connection");
  currentPlexConnection.hidden = !plex.configured;
  $("#plex-current-server").textContent =
    plex.serverName || (plex.configured ? "Plex Media Server" : "");
  $("#plex-current-url").textContent = plex.url || "";
  const selectedPlexLibraryIds = new Set(
    (plex.librarySectionIds || []).map((value: string | number) => String(value)),
  );
  const selectedPlexLibraryNames = (plex.libraries || [])
    .filter((library: JsonObject) => selectedPlexLibraryIds.has(String(library.id)))
    .map((library: JsonObject) => library.title);
  $("#plex-current-libraries").textContent =
    selectedPlexLibraryNames.join(", ") || (plex.configured ? "None selected" : "");
  populatePlexLibraries(plex.libraries || [], plex.librarySectionIds || []);

  const form = $<AppForm>("#lidarr-settings");
  form.apiKey.value = "";
  if (lidarr.url) {
    const connection = parseLidarrUrl(lidarr.url);
    form.hostname.value = connection.hostname;
    form.port.value = connection.port;
    form.useSsl.checked = connection.useSsl;
    form.externalUrl.value = lidarr.externalUrl || "";
    $("#lidarr-defaults").disabled = false;

    if (loadLidarrOptions) {
      try {
        populateLidarrOptions(await api("/api/lidarr/options"));
      } catch {
        // A saved configuration may no longer be reachable. The settings form
        // remains usable so the user can correct it.
      }
    }
  }

  form.monitor.value = lidarrDefaults.monitor || "all";
  form.monitorNewItems.value = lidarrDefaults.monitorNewItems || "all";
  form.searchForMissingAlbums.checked = lidarrDefaults.searchForMissingAlbums !== false;

  const status = $("#status");
  status.textContent = lidarr.configured
    ? `Lidarr connected${plex.configured ? " · Plex connected" : ""}`
    : "Connect Lidarr in Settings";
  status.className = `status ${lidarr.configured ? "ready" : "warn"}`;
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** unit);
  return `${value.toFixed(unit === 0 || value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function tableCell(row: HTMLTableRowElement, value: string) {
  const cell = document.createElement("td");
  cell.textContent = value;
  row.append(cell);
  return cell;
}

async function refreshMaintenance() {
  if (maintenanceRefreshInFlight) return;
  maintenanceRefreshInFlight = true;
  const jobsTable = $<HTMLTableSectionElement>("#jobs-table");
  const cacheTable = $<HTMLTableSectionElement>("#cache-table");
  const message = $("#maintenance-message");
  try {
    const data = await api("/api/settings/maintenance");
    jobsTable.replaceChildren();
    data.jobs.forEach((job: JsonObject) => {
      const row = document.createElement("tr");
      tableCell(row, job.name);
      const type = tableCell(row, "");
      const badge = document.createElement("span"); badge.className = "job-type"; badge.textContent = job.type; type.append(badge);
      tableCell(row, job.schedule);
      const statusCell = tableCell(row, "");
      const status = document.createElement("span");
      status.className = `job-status${job.running ? " running" : ""}`;
      if (job.running) {
        const progress = job.total ? ` · ${job.completed}/${job.total}` : "";
        status.textContent = `${job.phase && job.phase !== "idle" ? job.phase : "Running"}${progress}`;
      }
      else if (job.queued) status.textContent = `${job.queued} queued${job.retrying ? ` · ${job.retrying} retrying` : ""}`;
      else if (job.nextExecutionAt) status.textContent = `Next ${new Date(job.nextExecutionAt * 1000).toLocaleString()}`;
      else status.textContent = "Idle";
      statusCell.append(status);
      const actions = tableCell(row, "");
      const run = document.createElement("button");
      run.type = "button"; run.className = "run-job"; run.textContent = job.running ? "Running…" : "Run now"; run.disabled = Boolean(job.running);
      run.addEventListener("click", async () => {
        run.disabled = true;
        try {
          const result = await api(`/api/settings/jobs/${encodeURIComponent(job.id)}/run`, { method: "POST" });
          setMessage(message, result.message);
          await refreshMaintenance();
        } catch (error) { setMessage(message, error.message, true); }
        finally { run.disabled = false; }
      });
      actions.append(run);
      jobsTable.append(row);
    });

    cacheTable.replaceChildren();
    data.caches.forEach((cache: JsonObject) => {
      const row = document.createElement("tr");
      tableCell(row, cache.name);
      tableCell(row, Number(cache.entries || 0).toLocaleString());
      tableCell(row, Number(cache.expired || 0).toLocaleString());
      tableCell(row, formatBytes(Number(cache.valueBytes || 0)));
      tableCell(row, cache.latestExpiry ? new Date(cache.latestExpiry * 1000).toLocaleString() : "On demand");
      const actions = tableCell(row, "");
      const flush = document.createElement("button");
      flush.type = "button"; flush.textContent = "Flush cache";
      flush.addEventListener("click", async () => {
        if (!window.confirm(`Flush ${cache.name}? It will be rebuilt as Melodarr uses it.`)) return;
        flush.disabled = true;
        try {
          const result = await api(`/api/settings/cache/${encodeURIComponent(cache.id)}/flush`, { method: "POST" });
          setMessage(message, result.message);
          await refreshMaintenance();
        } catch (error) { setMessage(message, error.message, true); }
        finally { flush.disabled = false; }
      });
      actions.append(flush);
      cacheTable.append(row);
    });
    $("#metadata-cache-size").textContent = `Metadata DB · ${formatBytes(data.metadataDatabaseBytes)}`;
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    maintenanceRefreshInFlight = false;
  }
}

function showSettingsPage(page: SettingsPage, updateHistory = true) {
  document.querySelectorAll<HTMLElement>("[data-settings-page]").forEach((button) => button.classList.toggle("active", button.dataset.settingsPage === page));
  $("#settings-services").hidden = page !== "services";
  $("#settings-requests").hidden = page !== "requests";
  $("#settings-users").hidden = page !== "users";
  $("#settings-jobs").hidden = page !== "jobs";
  if (maintenanceRefreshTimer !== undefined) window.clearInterval(maintenanceRefreshTimer);
  maintenanceRefreshTimer = undefined;
  if (page === "jobs") {
    refreshMaintenance();
    maintenanceRefreshTimer = window.setInterval(refreshMaintenance, 10_000);
  } else if (page === "requests") {
    refreshAdminRequests();
  } else if (page === "users") {
    refreshAdminUsers();
  }
  if (updateHistory) {
    const path = page === "services" ? "/settings" : `/settings/${page}`;
    window.history.pushState({ view: "settings", settings: page }, "", path);
    resetPageScroll();
  }
}

function setupNavigation() {
  let activeAccountUsername = "";
  let activeAccountRequestPage = 1;

  function isOwnAccountUsername(username: string) {
    if (!currentUser) return false;
    const normalizedUsername = username.toLocaleLowerCase();
    return [currentUser.username, currentUser.plexUsername]
      .filter(Boolean)
      .some((candidate) => candidate!.toLocaleLowerCase() === normalizedUsername);
  }

  function showView(view: AppView, updateHistory = true) {
    if (!currentUser || (currentUser.role !== "admin" && !VIEWS_FOR_EVERY_USER.includes(view))) view = "discover";
    document.querySelectorAll(".nav-link, .view").forEach((element) => element.classList.remove("active"));
    // Account and detail are application views without a matching nav button,
    // and the header and bottom tab bar both carry a button per view.
    document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => {
      const isCurrent = button.dataset.view === view;
      button.classList.toggle("active", isCurrent);
      if (isCurrent) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    $(`#${view}`).classList.add("active");
    if (view === "library") {
      window.dispatchEvent(new Event("melodarr-library-visible"));
    }
    if (view !== "settings" && maintenanceRefreshTimer !== undefined) {
      window.clearInterval(maintenanceRefreshTimer);
      maintenanceRefreshTimer = undefined;
    }

    if (updateHistory) {
      const path = view === "discover" ? "/" : `/${view}`;
      window.history.pushState({ view }, "", path);
    }
    resetPageScroll();
  }

  function accountPath(
    page: AccountPage,
    username = activeAccountUsername || currentUser?.username || "",
    requestPage = activeAccountRequestPage,
  ) {
    if (!currentUser) throw new Error("Account navigation requires an authenticated user.");
    const encodedUsername = encodeURIComponent(username);
    if (page === "profile") return `/${encodedUsername}`;
    if (page === "requests") {
      const query = requestPage > 1 ? `?page=${requestPage}` : "";
      return `/${encodedUsername}/requests${query}`;
    }
    return `/${encodedUsername}/settings/${page}`;
  }

  function createHistoryItem(item: JsonObject, route: string) {
    const row = document.createElement("article");
    row.className = "history-item";

    const detailLink = document.createElement("a");
    detailLink.className = "history-detail";
    detailLink.href = `/${route}/${encodeURIComponent(item.mbid)}`;

    const copy = document.createElement("span");
    copy.className = "history-copy";
    const title = document.createElement("strong");
    title.className = "history-title";
    title.textContent = item.name;
    copy.append(title);

    if (route === "albums") {
      const releaseType = String(item.release_type || "");
      const metadata = [
        item.artist_name,
        releaseType ? releaseType[0].toUpperCase() + releaseType.slice(1) : "",
        item.release_date,
      ].filter(Boolean);
      if (metadata.length) {
        const secondary = document.createElement("span");
        secondary.className = "history-meta";
        secondary.textContent = metadata.join(" · ");
        copy.append(secondary);
      }
    }

    const requestedAtDate = new Date(Number(item.created_at) * 1000);
    const requestedAt = document.createElement("time");
    requestedAt.className = "history-request-date";
    requestedAt.dateTime = requestedAtDate.toISOString();
    requestedAt.textContent = requestedAtDate.toLocaleDateString();
    detailLink.append(copy, requestedAt);
    row.append(detailLink);

    if (item.availableInPlex) {
      const destination = mobilePlexDestination(
        String(item.plexUrl || ""),
        String(item.plexampUrl || ""),
      );
      const plexBadge: HTMLElement = destination.url
        ? document.createElement("a")
        : document.createElement("span");
      plexBadge.className = "history-plex";
      plexBadge.title = destination.url ? destination.label : "Available in Plex";
      plexBadge.setAttribute("aria-label", plexBadge.title);
      if (plexBadge instanceof HTMLAnchorElement) {
        plexBadge.href = destination.url;
        if (destination.openInNewTab) {
          plexBadge.target = "_blank";
          plexBadge.rel = "noreferrer";
        }
      }
      const icon = document.createElement("img");
      icon.src = "/icons/plex.svg";
      icon.alt = "";
      plexBadge.append(icon);
      row.append(plexBadge);
    }
    return row;
  }

  async function renderAccount(page: AccountPage) {
    const user = currentUser;
    if (!user) return;
    const targetUsername = activeAccountUsername || user.username;
    const isOwnAccount = isOwnAccountUsername(targetUsername);
    const content = $("#account-content");
    $("#account-title").textContent = page === "profile" ? "Profile" : page.split("-").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ");
    document.querySelectorAll<HTMLElement>("[data-account-route]").forEach((link) => link.classList.toggle("active", link.dataset.accountRoute === page));
    document.querySelectorAll<HTMLElement>("[data-account-owner-only]").forEach((element) => {
      element.hidden = !isOwnAccount || (
        element.classList.contains("admin-only") && user.role !== "admin"
      );
    });
    content.replaceChildren();
    const message = document.createElement("p");
    message.className = "message";
    message.textContent = "Loading…";
    content.append(message);
    try {
      if (page === "profile") {
        const data = await api(
          `/api/account/profile?username=${encodeURIComponent(targetUsername)}&page=1`,
        );
        content.replaceChildren();
        const identity = data.user as AdminUserIdentity;
        const section = document.createElement("section");
        section.className = "account-section profile-summary";
        const avatar = createUserAvatar(identity);
        avatar.classList.add("user-avatar-large");
        const copy = document.createElement("div");
        const heading = document.createElement("h2");
        heading.textContent = adminUserDisplayName(identity);
        const accountType = document.createElement("p");
        accountType.textContent = `${identity.userType === "plex" ? "Plex user" : "Local account"} · ${identity.role === "admin" ? "Administrator" : "User"}`;
        copy.append(heading, accountType);
        section.append(avatar, copy);

        const actions = document.createElement("div");
        actions.className = "profile-actions";
        const requestsLink = document.createElement("a");
        requestsLink.className = "outline";
        requestsLink.href = accountPath("requests", targetUsername, 1);
        requestsLink.textContent = "View requests";
        requestsLink.addEventListener("click", (event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          showAccountPage?.("requests", true, targetUsername, 1);
        });
        actions.append(requestsLink);
        content.append(section, actions);
      } else if (page === "requests") {
        const data = await api(
          `/api/account/profile?username=${encodeURIComponent(targetUsername)}&page=${encodeURIComponent(activeAccountRequestPage)}`,
        );
        content.replaceChildren();
        const requestGroups: [string, JsonObject[], string][] = [
          ["Artists", data.requests?.artist || [], "artists"],
          ["Release groups", data.requests?.["release-group"] || [], "albums"],
        ];
        requestGroups.forEach(([title, requests, route]) => {
          const section = document.createElement("section");
          section.className = "account-section";
          const heading = document.createElement("h2"); heading.textContent = title;
          const list = document.createElement("div"); list.className = "results";
          if (!requests.length) { const empty = document.createElement("p"); empty.className = "message"; empty.textContent = "No requests yet."; list.append(empty); }
          requests.forEach((item: JsonObject) => list.append(createHistoryItem(item, route)));
          section.append(heading, list); content.append(section);
        });

        const pagination = data.pagination || {
          page: activeAccountRequestPage,
          pageSize: 100,
          total: requestGroups.reduce((total, [, requests]) => total + requests.length, 0),
          totalPages: 1,
        };
        const paginationControls = document.createElement("nav");
        paginationControls.className = "request-pagination";
        paginationControls.setAttribute("aria-label", "Request history pages");
        const previous = document.createElement("button");
        previous.type = "button";
        previous.className = "outline";
        previous.textContent = "Previous";
        previous.disabled = pagination.page <= 1;
        const status = document.createElement("span");
        status.textContent = pagination.total
          ? `Page ${pagination.page.toLocaleString()} of ${pagination.totalPages.toLocaleString()} · ${pagination.total.toLocaleString()} requests`
          : "No requests";
        const next = document.createElement("button");
        next.type = "button";
        next.className = "outline";
        next.textContent = "Next";
        next.disabled = pagination.totalPages === 0 || pagination.page >= pagination.totalPages;
        const showRequestPage = (nextPage: number) => {
          showAccountPage?.("requests", true, targetUsername, nextPage);
        };
        previous.addEventListener("click", () => showRequestPage(pagination.page - 1));
        next.addEventListener("click", () => showRequestPage(pagination.page + 1));
        paginationControls.append(previous, status, next);
        content.append(paginationControls);
      } else if (page === "general") {
        content.replaceChildren();
        const form = document.createElement("form") as AppForm; form.className = "service-card account-form";
        form.innerHTML = '<h2>General</h2><label>Username<input name="username" autocomplete="username" required></label><label>New password<small>Leave blank to keep your current password.</small><input name="password" type="password" autocomplete="new-password" minlength="12"></label><div class="form-actions"><p class="form-message"></p><button>Save general settings</button></div>';
        form.username.value = user.username;
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const formMessage = requiredDescendant<HTMLElement>(form, ".form-message");
          try {
            const result = await api("/api/account/general", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(Object.fromEntries(new FormData(form))),
            });
            user.username = result.username;
            activeAccountUsername = result.username;
            const accountMenu = $<HTMLAnchorElement>("#account-menu");
            accountMenu.textContent = result.username.slice(0, 1).toUpperCase();
            accountMenu.href = accountPath("profile");
            formMessage.textContent = result.message;
            window.history.replaceState(
              { account: "general" },
              "",
              accountPath("general"),
            );
          } catch (error) {
            setMessage(formMessage, error.message, true);
          }
        });
        content.append(form);
      } else if (page === "linked-accounts") {
        const accountSettings = await api("/api/account/settings");
        content.replaceChildren();
        const form = document.createElement("form") as AppForm; form.className = "service-card account-form";
        form.innerHTML = `
          <h2>Linked accounts</h2>
          <fieldset>
            <legend>Plex</legend>
            <div class="linked-account-summary">
              <img src="/icons/plex.svg" width="42" height="42" alt="">
              <div>
                <strong id="account-plex-status"></strong>
                <small id="account-plex-detail"></small>
              </div>
              <button id="account-link-plex" class="plex-button" type="button">
                Link with <img src="/icons/plex.svg" alt="Plex">
              </button>
            </div>
            <p id="account-plex-message" class="form-message" aria-live="polite"></p>
          </fieldset>
          <fieldset>
            <legend>ListenBrainz</legend>
            <label>Username<small>Used to tailor recommendations. Leave blank to disconnect it.</small><input name="listenbrainzUsername" autocomplete="username" placeholder="your-listenbrainz-name"></label>
          </fieldset>
          <fieldset>
            <legend>Last.fm</legend>
            <label>Username<input name="lastfmUsername" autocomplete="username" placeholder="your-lastfm-name"></label>
            <label>API key<small>Create one in your Last.fm API account. Leave blank to keep your saved key.</small><input name="lastfmApiKey" type="password" autocomplete="off" placeholder="Last.fm API key"></label>
          </fieldset>
          <div class="form-actions"><p class="form-message"></p><button>Save linked accounts</button></div>
        `;
        user.plexLinked = Boolean(accountSettings.plexLinked);
        user.plexUsername = accountSettings.plexUsername || "";
        user.plexEmail = accountSettings.plexEmail || "";
        form.listenbrainzUsername.value = accountSettings.listenbrainzUsername || "";
        form.lastfmUsername.value = accountSettings.lastfmUsername || "";
        const plexStatus = requiredDescendant<HTMLElement>(form, "#account-plex-status");
        const plexDetail = requiredDescendant<HTMLElement>(form, "#account-plex-detail");
        const plexButton = requiredDescendant<HTMLButtonElement>(form, "#account-link-plex");
        const plexMessage = requiredDescendant<HTMLElement>(form, "#account-plex-message");
        const renderPlexLinkStatus = () => {
          if (user.plexLinked) {
            plexStatus.textContent = user.plexUsername || user.plexEmail || "Plex account linked";
            plexDetail.textContent = "You can sign in with either your local credentials or Plex.";
            plexButton.hidden = true;
          } else if (accountSettings.plexConfigured) {
            plexStatus.textContent = "Not linked";
            plexDetail.textContent = "Link a Plex account that has access to this server.";
            plexButton.hidden = false;
            plexButton.disabled = false;
          } else {
            plexStatus.textContent = "Plex is not configured";
            plexDetail.textContent = "An administrator must connect a Plex server first.";
            plexButton.hidden = false;
            plexButton.disabled = true;
          }
        };
        renderPlexLinkStatus();
        plexButton.addEventListener("click", async () => {
          plexButton.disabled = true;
          await startPlexAuthentication("link", plexMessage, async (result) => {
            user.plexLinked = true;
            user.plexUsername = result.plexUsername || "";
            user.plexEmail = result.plexEmail || "";
            renderPlexLinkStatus();
            setMessage(plexMessage, result.message);
          });
          if (!user.plexLinked && accountSettings.plexConfigured) {
            plexButton.disabled = false;
          }
        });
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const formMessage = requiredDescendant<HTMLElement>(form, ".form-message");
          setMessage(formMessage, "Saving linked accounts…");
          try {
            const [listenbrainz, lastfm] = await Promise.all([
              api("/api/account/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  username: form.listenbrainzUsername.value,
                }),
              }),
              api("/api/account/lastfm", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  username: form.lastfmUsername.value,
                  apiKey: form.lastfmApiKey.value,
                }),
              }),
            ]);
            user.listenbrainzUsername = form.listenbrainzUsername.value.trim();
            user.lastfmUsername = form.lastfmUsername.value.trim();
            user.lastfmConfigured = Boolean(user.lastfmUsername);
            form.lastfmApiKey.value = "";
            setMessage(
              formMessage,
              `${listenbrainz.message} ${lastfm.message} Recommendations are being refreshed.`,
            );
            window.dispatchEvent(new Event("melodarr-recommendations-changed"));
          } catch (error) {
            setMessage(formMessage, error.message, true);
          }
        });
        content.append(form);
      } else if (page === "invitations") {
        content.replaceChildren();
        const form = document.createElement("form") as AppForm; form.className = "service-card account-form";
        form.innerHTML = '<h2>Account invitations</h2><p class="intro">Create a private, one-time signup link. Each link expires after seven days.</p><div class="form-actions"><p class="form-message"></p><button>Create invitation link</button></div><div class="invitation-result" hidden><label>Invitation link<input name="invitationLink" readonly></label><button class="outline" type="button">Copy link</button></div>';
        const formMessage = requiredDescendant<HTMLElement>(form, ".form-message");
        const result = requiredDescendant<HTMLElement>(form, ".invitation-result");
        const linkInput = form.invitationLink;
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          result.hidden = true;
          setMessage(formMessage, "Creating invitation…");
          try {
            const invitation = await api("/api/account/invitations", { method: "POST" });
            linkInput.value = new URL(invitation.path, window.location.origin).href;
            result.hidden = false;
            setMessage(formMessage, `This one-time link expires ${new Date(invitation.expiresAt * 1000).toLocaleString()}.`);
          } catch (error) { setMessage(formMessage, error.message, true); }
        });
        requiredDescendant<HTMLButtonElement>(result, "button").addEventListener("click", async () => {
          const copied = await copyInputValue(linkInput);
          setMessage(
            formMessage,
            copied ? "Invitation link copied." : "Copy the selected invitation link.",
          );
        });
        content.append(form);
      }
    } catch (error) { setMessage(message, error.message, true); }
  }

  showAccountPage = (
    page = "profile",
    updateHistory = true,
    username = activeAccountUsername || currentUser?.username,
    requestPage = 1,
  ) => {
    if (!currentUser || !username) return;
    const isOwnAccount = isOwnAccountUsername(username);
    if (!isOwnAccount && currentUser.role !== "admin") {
      showView("discover");
      return;
    }
    activeAccountUsername = username;
    activeAccountRequestPage = page === "requests" ? Math.max(1, requestPage) : 1;
    showView("account", false);
    if (updateHistory) {
      window.history.pushState(
        { account: page, username, page: activeAccountRequestPage },
        "",
        accountPath(page, username, activeAccountRequestPage),
      );
    }
    const allowedPages: AccountPage[] = ["profile", "requests"];
    if (isOwnAccount) allowedPages.push("general", "linked-accounts");
    if (isOwnAccount && currentUser.role === "admin") allowedPages.push("invitations");
    renderAccount(allowedPages.includes(page) ? page : "profile");
  };

  document.querySelectorAll<HTMLElement>(".nav-link").forEach((button) => {
    button.addEventListener("click", () => {
      showView(button.dataset.view as AppView);
      if (button.dataset.view === "discover") {
        window.dispatchEvent(new Event("melodarr-home"));
      } else if (button.dataset.view === "settings" && currentUser?.role === "admin") {
        showSettingsPage("services", false);
        refreshSettings(true).catch(() => {});
      }
    });
  });

  $(".brand").addEventListener("click", (event) => {
    // Keep the real href as a no-JavaScript fallback, but avoid reloading the
    // entire application when an authenticated user returns home.
    if (!currentUser || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showView("discover");
    window.dispatchEvent(new Event("melodarr-home"));
  });

  window.addEventListener("popstate", () => {
    if (["/settings", "/settings/requests", "/settings/users", "/settings/jobs"].includes(window.location.pathname)) {
      showView("settings", false);
      const page: SettingsPage = window.location.pathname.endsWith("/requests")
        ? "requests"
        : window.location.pathname.endsWith("/users")
          ? "users"
          : window.location.pathname.endsWith("/jobs") ? "jobs" : "services";
      showSettingsPage(page, false);
      return;
    }
    const accountMatch = window.location.pathname.match(
      /^\/([^/]+)(?:\/(requests)|\/settings\/(general|linked-accounts|invitations))?\/?$/,
    );
    const accountUsername = accountMatch ? decodeURIComponent(accountMatch[1]) : "";
    const canViewAccount = accountMatch && currentUser && (
      isOwnAccountUsername(accountUsername)
      || currentUser.role === "admin"
    );
    if (accountMatch && canViewAccount) {
      const accountPage = (accountMatch[2] || accountMatch[3] || "profile") as AccountPage;
      const requestPage = accountPage === "requests"
        ? Math.max(1, Number.parseInt(new URLSearchParams(window.location.search).get("page") || "1", 10) || 1)
        : 1;
      showAccountPage?.(accountPage, false, accountUsername, requestPage);
      return;
    }
    const view = window.location.pathname.slice(1) || "discover";
    showView((["discover", "library", "settings"].includes(view) ? view : "discover") as AppView, false);
  });

  const initialView = window.location.pathname.slice(1) || "discover";
  if (["settings/requests", "settings/users", "settings/jobs"].includes(initialView)) showView("settings", false);
  else if (["library", "settings"].includes(initialView)) showView(initialView as AppView, false);

  document.querySelectorAll<HTMLElement>(".tab-bar .nav-link").forEach((button) => button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }));

  $<HTMLAnchorElement>("#account-menu").addEventListener("click", (event) => {
    if (!currentUser || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showAccountPage?.("profile", true, currentUser.username);
  });
  document.querySelectorAll<HTMLElement>("[data-account-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); showAccountPage?.(link.dataset.accountRoute as AccountPage); }));
  document.querySelector("#account-logout")?.addEventListener("click", () => signOut());
  document.querySelectorAll<HTMLElement>("[data-settings-page]").forEach((button) => button.addEventListener("click", () => showSettingsPage(button.dataset.settingsPage as SettingsPage)));
  document.querySelector("#refresh-maintenance")?.addEventListener("click", () => refreshMaintenance());
}

async function applyCurrentUser(user: CurrentUser) {
  currentUser = user;
  document.body.classList.add("authenticated");
  updateSessionChrome();
  try {
    await loadDiscovery();
    window.dispatchEvent(new Event("melodarr-authenticated"));
    // Re-evaluate a bookmarked view or detail route only after its API calls
    // have an authenticated session and the lazy discovery route is ready.
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch (error) {
    showToast(error.message, true);
  }
}

function adminUserDisplayName(user: AdminUserIdentity) {
  if (user.userType === "plex") {
    return user.plexUsername || user.username || user.plexEmail || "Plex user";
  }
  return user.localUsername || user.username || "Local user";
}

function createUserAvatar(user: AdminUserIdentity, large = false) {
  const avatar = document.createElement("span");
  avatar.className = `user-avatar${large ? " user-avatar-large" : ""}`;
  const displayName = adminUserDisplayName(user);
  avatar.textContent = displayName.slice(0, 1).toLocaleUpperCase() || "?";
  if (user.plexAvatar) {
    const image = document.createElement("img");
    image.src = user.plexAvatar;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", () => image.remove(), { once: true });
    avatar.append(image);
  }
  return avatar;
}

function joinedDate(value: number | string) {
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric * (numeric < 10_000_000_000 ? 1_000 : 1))
    : new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function createAdminRequestPlexBadge(item: AdminRequest) {
  if (!item.availableInPlex) return undefined;
  const destination = mobilePlexDestination(
    String(item.plexUrl || ""),
    String(item.plexampUrl || ""),
  );
  const badge: HTMLElement = destination.url
    ? document.createElement("a")
    : document.createElement("span");
  badge.className = "history-plex admin-request-plex";
  badge.title = destination.url ? destination.label : "Available in Plex";
  badge.setAttribute("aria-label", badge.title);
  if (badge instanceof HTMLAnchorElement) {
    badge.href = destination.url;
    if (destination.openInNewTab) {
      badge.target = "_blank";
      badge.rel = "noreferrer";
    }
  }
  const icon = document.createElement("img");
  icon.src = "/icons/plex.svg";
  icon.alt = "";
  badge.append(icon);
  return badge;
}

function createAdminRequestItem(item: AdminRequest) {
  const row = document.createElement("article");
  row.className = "admin-request-item";

  const detail = document.createElement("a");
  detail.className = "admin-request-detail";
  const route = item.kind === "artist" ? "artists" : "albums";
  detail.href = `/${route}/${encodeURIComponent(item.mbid)}`;

  const kind = document.createElement("span");
  kind.className = `admin-request-kind ${item.kind === "artist" ? "artist" : "release"}`;
  kind.textContent = item.kind === "artist" ? "Artist" : "Release group";

  const copy = document.createElement("span");
  copy.className = "history-copy";
  const title = document.createElement("strong");
  title.className = "history-title";
  title.textContent = item.name;
  copy.append(title);
  if (item.kind === "release-group") {
    const releaseType = String(item.release_type || "");
    const metadata = [
      item.artist_name,
      releaseType ? releaseType[0].toUpperCase() + releaseType.slice(1) : "",
      item.release_date,
    ].filter(Boolean);
    if (metadata.length) {
      const secondary = document.createElement("span");
      secondary.className = "history-meta";
      secondary.textContent = metadata.join(" · ");
      copy.append(secondary);
    }
  }
  detail.append(kind, copy);

  const requester = document.createElement("div");
  requester.className = "admin-request-requester";
  const requesterCopy = document.createElement("span");
  const requesterName = document.createElement("a");
  requesterName.className = "admin-request-requester-link";
  requesterName.href = `/${encodeURIComponent(adminUserDisplayName(item.requester))}`;
  requesterName.textContent = adminUserDisplayName(item.requester);
  const requesterMeta = document.createElement("small");
  const requestedAtDate = joinedDate(item.created_at);
  const accountType = item.requester.userType === "plex" ? "Plex user" : "Local account";
  if (requestedAtDate) {
    const requestedAt = document.createElement("time");
    requestedAt.dateTime = requestedAtDate.toISOString();
    requestedAt.title = requestedAtDate.toLocaleString();
    requestedAt.textContent = requestedAtDate.toLocaleDateString();
    requesterMeta.append(`${accountType} · `, requestedAt);
  } else {
    requesterMeta.textContent = accountType;
  }
  requesterCopy.append(requesterName, requesterMeta);
  requester.append(createUserAvatar(item.requester), requesterCopy);

  row.append(detail, requester);
  const plexBadge = createAdminRequestPlexBadge(item);
  if (plexBadge) row.append(plexBadge);
  return row;
}

function setAdminRequestStats() {
  $("#admin-requests-total").textContent = adminRequestsPagination.total.toLocaleString();
  $("#admin-requests-artists").textContent = adminRequests
    .filter((item) => item.kind === "artist").length.toLocaleString();
  $("#admin-requests-releases").textContent = adminRequests
    .filter((item) => item.kind === "release-group").length.toLocaleString();
}

function renderAdminRequestPagination() {
  const pagination = $("#admin-requests-pagination");
  const previous = $<HTMLButtonElement>("#admin-requests-previous");
  const next = $<HTMLButtonElement>("#admin-requests-next");
  const pageLabel = $("#admin-requests-page");
  const { page, totalPages } = adminRequestsPagination;
  pagination.hidden = totalPages <= 1;
  previous.disabled = page <= 1;
  next.disabled = totalPages === 0 || page >= totalPages;
  pageLabel.textContent = `Page ${page.toLocaleString()} of ${Math.max(totalPages, 1).toLocaleString()}`;
}

function renderAdminRequests() {
  const list = $("#admin-requests-list");
  const search = $<HTMLInputElement>("#admin-requests-search");
  const kind = $<HTMLSelectElement>("#admin-requests-type").value;
  const query = normalizeSearch(search.value);
  const visibleRequests = adminRequests.filter((item) => {
    if (kind !== "all" && item.kind !== kind) return false;
    if (!query) return true;
    return normalizeSearch([
      item.name,
      item.artist_name,
      item.release_type,
      item.release_date,
      adminUserDisplayName(item.requester),
      item.requester.localUsername,
      item.requester.plexEmail,
    ].filter(Boolean).join(" ")).includes(query);
  });
  list.replaceChildren();

  if (!visibleRequests.length) {
    const empty = document.createElement("p");
    empty.className = "message admin-request-empty";
    empty.textContent = adminRequests.length
      ? "No requests match the current filters."
      : "No requests have been made yet.";
    list.append(empty);
  } else {
    visibleRequests.forEach((item) => list.append(createAdminRequestItem(item)));
  }

  const filtered = query || kind !== "all";
  const { page, pageSize, total } = adminRequestsPagination;
  const firstResult = total ? ((page - 1) * pageSize) + 1 : 0;
  const lastResult = total ? firstResult + adminRequests.length - 1 : 0;
  setMessage(
    $("#admin-requests-message"),
    filtered
      ? `${visibleRequests.length.toLocaleString()} of ${adminRequests.length.toLocaleString()} requests on this page.`
      : total
        ? `Showing ${firstResult.toLocaleString()}–${lastResult.toLocaleString()} of ${total.toLocaleString()} requests.`
        : "No requests.",
  );
  renderAdminRequestPagination();
}

async function refreshAdminRequests(page = adminRequestsPagination.page) {
  const request = ++adminRequestsRequest;
  const list = $("#admin-requests-list");
  list.replaceChildren(skeletonBlock("admin-request-item", 6));
  setMessage($("#admin-requests-message"), "Loading requests…");
  $<HTMLButtonElement>("#admin-requests-previous").disabled = true;
  $<HTMLButtonElement>("#admin-requests-next").disabled = true;
  try {
    const result = await api<{
      requests: AdminRequest[];
      pagination: AdminRequestPagination;
    }>(`/api/admin/requests?page=${encodeURIComponent(page)}`);
    if (request !== adminRequestsRequest) return;
    adminRequests = result.requests || [];
    adminRequestsPagination = result.pagination || {
      page,
      pageSize: 100,
      total: adminRequests.length,
      totalPages: adminRequests.length ? 1 : 0,
    };
    setAdminRequestStats();
    renderAdminRequests();
  } catch (error) {
    if (request !== adminRequestsRequest) return;
    adminRequests = [];
    adminRequestsPagination = {
      page,
      pageSize: 100,
      total: 0,
      totalPages: 0,
    };
    setAdminRequestStats();
    renderAdminRequestPagination();
    list.replaceChildren();
    const errorState = document.createElement("div");
    errorState.className = "admin-request-load-error";
    const copy = document.createElement("p");
    copy.className = "message error";
    copy.textContent = error.message;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "outline";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => refreshAdminRequests());
    errorState.append(copy, retry);
    list.append(errorState);
    setMessage($("#admin-requests-message"), "Requests could not be loaded.", true);
  }
}

function setupAdminRequests() {
  $("#refresh-admin-requests").addEventListener("click", () => refreshAdminRequests());
  $("#admin-requests-previous").addEventListener("click", () => {
    if (adminRequestsPagination.page <= 1) return;
    refreshAdminRequests(adminRequestsPagination.page - 1);
  });
  $("#admin-requests-next").addEventListener("click", () => {
    if (adminRequestsPagination.page >= adminRequestsPagination.totalPages) return;
    refreshAdminRequests(adminRequestsPagination.page + 1);
  });
  $<HTMLInputElement>("#admin-requests-search").addEventListener(
    "input",
    () => renderAdminRequests(),
  );
  $<HTMLSelectElement>("#admin-requests-type").addEventListener(
    "change",
    () => renderAdminRequests(),
  );
}

function updateSessionChrome() {
  if (!currentUser) return;
  const user = currentUser;
  const isAdmin = user.role === "admin";
  document.querySelectorAll<HTMLElement>(".admin-only").forEach((element) => {
    element.hidden = !isAdmin;
  });
  const status = $("#status");
  status.textContent = `Signed in as ${user.username}${isAdmin ? " · Administrator" : ""}`;
  status.className = "status ready";
  const accountMenu = $<HTMLAnchorElement>("#account-menu");
  accountMenu.textContent = user.username.slice(0, 1).toUpperCase();
  accountMenu.href = `/${encodeURIComponent(user.username)}`;
  accountMenu.setAttribute("aria-label", `Open settings for ${user.username}`);
}

function isCurrentSessionUser(user: AdminUser) {
  if (!currentUser) return false;
  if (currentUser.id !== undefined) return currentUser.id === user.id;
  const currentName = currentUser.username.toLocaleLowerCase();
  return [user.username, user.localUsername, user.plexUsername]
    .filter(Boolean)
    .some((name) => name!.toLocaleLowerCase() === currentName);
}

function renderAdminUsers() {
  const table = $<HTMLTableSectionElement>("#users-table");
  const query = normalizeSearch($<HTMLInputElement>("#users-search").value);
  const visibleUsers = adminUsers.filter((user) => normalizeSearch([
    adminUserDisplayName(user),
    user.localUsername,
    user.plexEmail,
    user.userType,
    user.role,
  ].filter(Boolean).join(" ")).includes(query));
  table.replaceChildren();

  if (!visibleUsers.length) {
    const row = document.createElement("tr");
    const empty = tableCell(
      row,
      adminUsers.length ? `No users match “${$<HTMLInputElement>("#users-search").value.trim()}”.` : "No users have joined Melodarr yet.",
    );
    empty.className = "table-empty";
    empty.colSpan = 6;
    table.append(row);
    return;
  }

  visibleUsers.forEach((user) => {
    const row = document.createElement("tr");
    const identityCell = tableCell(row, "");
    identityCell.dataset.label = "Username";
    const identity = document.createElement("div");
    identity.className = "user-identity";
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = adminUserDisplayName(user);
    copy.append(name);
    const secondaryParts = user.userType === "plex"
      ? [user.localUsername ? `Local: ${user.localUsername}` : "", user.plexEmail]
      : [];
    const secondaryText = secondaryParts.filter(Boolean).join(" · ");
    if (secondaryText) {
      const secondary = document.createElement("small");
      secondary.textContent = secondaryText;
      copy.append(secondary);
    }
    identity.append(createUserAvatar(user), copy);
    identityCell.append(identity);

    const requests = tableCell(row, "");
    requests.dataset.label = "Requests";
    requests.className = "user-request-count";
    const requestLink = document.createElement("a");
    requestLink.href = `/${encodeURIComponent(adminUserDisplayName(user))}/requests`;
    requestLink.textContent = Number(user.requestCount || 0).toLocaleString();
    requestLink.setAttribute(
      "aria-label",
      `View requests from ${adminUserDisplayName(user)}`,
    );
    requests.append(requestLink);

    const typeCell = tableCell(row, "");
    typeCell.dataset.label = "User type";
    const type = document.createElement("span");
    type.className = `user-badge user-type ${user.userType}`;
    type.textContent = user.userType === "plex" ? "Plex user" : "Local account";
    typeCell.append(type);

    const roleCell = tableCell(row, "");
    roleCell.dataset.label = "Role";
    const role = document.createElement("span");
    role.className = `user-badge user-role ${user.role}`;
    role.textContent = user.role === "admin" ? "Administrator" : "User";
    roleCell.append(role);

    const joinedCell = tableCell(row, "");
    joinedCell.dataset.label = "Joined";
    const date = joinedDate(user.joinedAt);
    if (date) {
      const time = document.createElement("time");
      time.dateTime = date.toISOString();
      time.textContent = date.toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
      joinedCell.append(time);
    } else {
      joinedCell.textContent = "Unknown";
    }

    const actionCell = tableCell(row, "");
    actionCell.className = "user-actions";
    const actionButtons = document.createElement("div");
    actionButtons.className = "user-action-buttons";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "outline edit-user";
    edit.textContent = "Edit";
    edit.setAttribute("aria-label", `Edit settings for ${adminUserDisplayName(user)}`);
    edit.addEventListener("click", () => openAdminUserDialog(user));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "outline delete-user";
    remove.textContent = "Delete";
    remove.setAttribute("aria-label", `Delete ${adminUserDisplayName(user)}`);
    const currentSession = isCurrentSessionUser(user);
    remove.disabled = currentSession;
    if (currentSession) {
      remove.title = "You cannot delete your own account.";
    }
    remove.addEventListener("click", () => deleteAdminUser(user, remove));
    actionButtons.append(edit, remove);
    actionCell.append(actionButtons);
    table.append(row);
  });
}

function setAdminUserStats() {
  $("#users-total").textContent = adminUsers.length.toLocaleString();
  $("#users-plex").textContent = adminUsers.filter((user) => user.userType === "plex").length.toLocaleString();
  $("#users-local").textContent = adminUsers.filter((user) => user.userType === "local").length.toLocaleString();
}

async function refreshAdminUsers() {
  const request = ++adminUsersRequest;
  const table = $<HTMLTableSectionElement>("#users-table");
  const message = $("#users-message");
  table.innerHTML = '<tr><td colspan="6" class="table-empty">Loading users…</td></tr>';
  setMessage(message, "");
  try {
    const result = await api<{ users: AdminUser[] }>("/api/admin/users");
    if (request !== adminUsersRequest) return;
    adminUsers = result.users || [];
    setAdminUserStats();
    renderAdminUsers();
    setMessage(
      message,
      `${adminUsers.length.toLocaleString()} ${adminUsers.length === 1 ? "user" : "users"}.`,
    );
  } catch (error) {
    if (request !== adminUsersRequest) return;
    adminUsers = [];
    setAdminUserStats();
    table.replaceChildren();
    const row = document.createElement("tr");
    const cell = tableCell(row, "");
    cell.colSpan = 6;
    cell.className = "table-empty user-load-error";
    const copy = document.createElement("span");
    copy.textContent = error.message;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "outline";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => refreshAdminUsers());
    cell.append(copy, retry);
    table.append(row);
    setMessage(message, "Users could not be loaded.", true);
  }
}

async function deleteAdminUser(user: AdminUser, button: HTMLButtonElement) {
  const displayName = adminUserDisplayName(user);
  if (!window.confirm(
    `Delete ${displayName}? This permanently removes their account, request history, invitations, and queued work. This cannot be undone.`,
  )) return;

  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Deleting…";
  try {
    await api(`/api/admin/users/${encodeURIComponent(String(user.id))}`, {
      method: "DELETE",
    });
    adminUsers = adminUsers.filter((candidate) => candidate.id !== user.id);
    setAdminUserStats();
    renderAdminUsers();
    const message = `Deleted ${displayName}.`;
    setMessage($("#users-message"), message);
    showToast(message);
  } catch (error) {
    setMessage($("#users-message"), error.message, true);
    showToast(error.message, true);
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }
}

function openAdminUserDialog(user: AdminUser) {
  editingAdminUser = user;
  const dialog = $<HTMLDialogElement>("#user-dialog");
  const form = $<AdminUserForm>("#user-form");
  const currentSession = isCurrentSessionUser(user);
  form.reset();
  form.role.value = user.role;
  form.role.disabled = currentSession;
  form.localUsername.value = user.localUsername || (user.userType === "local" ? user.username : "");
  form.listenbrainzUsername.value = user.listenbrainzUsername || "";
  form.lastfmUsername.value = user.lastfmUsername || "";
  $("#user-dialog-title").textContent = `Edit ${adminUserDisplayName(user)}`;
  $("#user-dialog-name").textContent = adminUserDisplayName(user);
  $("#user-dialog-account").textContent = user.userType === "plex"
    ? [user.plexEmail, "Plex account"].filter(Boolean).join(" · ")
    : "Local account";
  $("#user-local-help").textContent = user.userType === "plex"
    ? "Generated for this Plex account. You can change the username used for local sign-in."
    : "Used for local sign-in.";
  $("#user-password-help").textContent = user.userType === "plex"
    ? "Set a password to enable or reset local sign-in. Leave blank to keep the current access."
    : "Leave blank to keep the current password. Passwords must be at least 12 characters.";
  $("#user-role-help").textContent = currentSession
    ? "You cannot change your own role while signed in."
    : "Controls access to administrative settings.";
  const replacement = createUserAvatar(user, true);
  replacement.id = "user-dialog-avatar";
  $("#user-dialog-avatar").replaceWith(replacement);
  setMessage($("#user-dialog-message"), "");
  dialog.showModal();
}

function setupAdminUsers() {
  const dialog = $<HTMLDialogElement>("#user-dialog");
  const form = $<AdminUserForm>("#user-form");
  const saveButton = $<HTMLButtonElement>("#save-user");
  const dialogMessage = $("#user-dialog-message");

  $("#refresh-users").addEventListener("click", () => refreshAdminUsers());
  $<HTMLInputElement>("#users-search").addEventListener("input", () => renderAdminUsers());
  document.querySelectorAll<HTMLElement>(".close-user-dialog").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    editingAdminUser = undefined;
    form.reset();
    setMessage(dialogMessage, "");
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const user = editingAdminUser;
    if (!user) return;
    const wasCurrentSession = isCurrentSessionUser(user);
    const payload = {
      role: form.role.value,
      localUsername: form.localUsername.value.trim(),
      password: form.password.value,
      listenbrainzUsername: form.listenbrainzUsername.value.trim(),
      lastfmUsername: form.lastfmUsername.value.trim(),
      lastfmApiKey: form.lastfmApiKey.value,
    };
    saveButton.disabled = true;
    form.setAttribute("aria-busy", "true");
    setMessage(dialogMessage, "Saving user settings…");
    try {
      const result = await api<{ user?: AdminUser; message?: string } & Partial<AdminUser>>(
        `/api/admin/users/${encodeURIComponent(String(user.id))}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      const responseUser = result.user || result;
      const updated: AdminUser = {
        ...user,
        role: payload.role as AdminUser["role"],
        localUsername: payload.localUsername,
        listenbrainzUsername: payload.listenbrainzUsername,
        lastfmUsername: payload.lastfmUsername,
        ...(responseUser as Partial<AdminUser>),
      };
      if (updated.userType === "local" && !responseUser.username && payload.localUsername) {
        updated.username = payload.localUsername;
      }
      adminUsers = adminUsers.map((candidate) => candidate.id === updated.id ? updated : candidate);
      setAdminUserStats();
      renderAdminUsers();

      if (wasCurrentSession && currentUser) {
        currentUser.username = updated.localUsername || currentUser.username;
        currentUser.plexUsername = updated.plexUsername;
        currentUser.plexEmail = updated.plexEmail;
        currentUser.listenbrainzUsername = updated.listenbrainzUsername;
        currentUser.lastfmUsername = updated.lastfmUsername;
        currentUser.lastfmConfigured = updated.lastfmConfigured;
        updateSessionChrome();
      }

      dialog.close();
      const successMessage = result.message || `Saved settings for ${adminUserDisplayName(updated)}.`;
      setMessage($("#users-message"), successMessage);
      showToast(successMessage);
      if (wasCurrentSession && currentUser?.role !== "admin") {
        window.history.replaceState({ view: "discover" }, "", "/");
        window.dispatchEvent(new PopStateEvent("popstate"));
      }
    } catch (error) {
      setMessage(dialogMessage, error.message, true);
    } finally {
      saveButton.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
}

function showSetupPanel(
  panel: "choice" | "plex-login" | "local-account" | "plex-server",
) {
  const panelIds = {
    choice: "setup-choice",
    "plex-login": "setup-plex-login",
    "local-account": "setup-local-account",
    "plex-server": "setup-plex-server",
  };
  Object.values(panelIds).forEach((id) => {
    $(`#${id}`).hidden = id !== panelIds[panel];
  });
  const step = panel === "choice" ? 1 : panel === "plex-server" ? 3 : 2;
  const localPath = panel === "local-account";
  document.querySelectorAll<HTMLElement>("[data-setup-step]").forEach((item) => {
    const itemStep = Number(item.dataset.setupStep);
    item.hidden = localPath && itemStep === 3;
    item.classList.toggle("active", itemStep === step);
    item.classList.toggle("completed", itemStep < step);
  });
  const secondLabel = document.querySelector<HTMLElement>(
    '[data-setup-step="2"] strong',
  );
  if (secondLabel) secondLabel.textContent = localPath ? "Admin account" : "Sign in";
}

async function showAuth({ resetPath = false } = {}) {
  currentUser = undefined;
  document.body.classList.remove("authenticated");
  const authCard = $("#auth-card");
  const setupWizard = $("#setup-wizard");
  const loginForm = $("#login-form");
  const registerForm = $("#register-form");
  const plexLoginOption = $("#plex-login-option");
  loginForm.reset();
  registerForm.reset();
  setMessage(requiredDescendant(loginForm, ".form-message"), "");
  setMessage(requiredDescendant(registerForm, ".form-message"), "");
  setMessage(requiredDescendant(plexLoginOption, ".form-message"), "");
  $<HTMLButtonElement>("#plex-login").disabled = false;
  loginForm.hidden = true;
  registerForm.hidden = true;
  plexLoginOption.hidden = true;
  authCard.hidden = false;
  setupWizard.hidden = true;
  if (resetPath) window.history.replaceState({}, "", "/");

  const parameters = new URLSearchParams(window.location.search);
  invitationToken = parameters.get("invite") || "";
  try {
    const query = invitationToken ? `?invite=${encodeURIComponent(invitationToken)}` : "";
    const status = await api(`/api/auth/status${query}`);
    if (status.firstAccount) {
      invitationToken = "";
      window.history.replaceState({ setup: true }, "", "/setup");
      authCard.hidden = true;
      setupWizard.hidden = false;
      showSetupPanel("choice");
    } else if (invitationToken && status.invitationValid) {
      $("#auth-title").innerHTML = "You’re<br><em>invited.</em>";
      $("#auth-intro").textContent = "Create your account using this one-time invitation.";
      $("#register-title").textContent = "Create invited account";
      registerForm.hidden = false;
    } else {
      if (window.location.pathname === "/setup" || window.location.pathname === "/register") {
        window.history.replaceState({}, "", "/");
      }
      $("#auth-title").innerHTML = "Music, for<br><em>your people.</em>";
      $("#auth-intro").textContent = invitationToken
        ? "That invitation is invalid, expired, or already used. Ask an administrator for a new link."
        : "Sign in to discover and request music.";
      invitationToken = "";
      loginForm.hidden = false;
      plexLoginOption.hidden = !status.plexConfigured;
    }
  } catch (error) {
    $("#auth-intro").textContent = error.message;
    loginForm.hidden = false;
  }
}

async function completeAuthentication(endpoint: string, form: HTMLFormElement, message: Element, extra: JsonObject = {}) {
  const body = { ...Object.fromEntries(new FormData(form)), ...extra };
  try {
    const user = await api<CurrentUser>(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (endpoint === "/api/auth/register") {
      // Remove the one-time invitation bearer token from browser history as
      // soon as it has been consumed.
      window.history.replaceState({ view: "discover" }, "", "/");
    }
    await applyCurrentUser(user);
    form.reset();
    if (user.role === "admin") await refreshSettings(window.location.pathname.startsWith("/settings"));
  } catch (error) {
    setMessage(message, error.message, true);
  }
}

function setupAuth() {
  const loginForm = $<AppForm>("#login-form");
  const registerForm = $<AppForm>("#register-form");
  const setupLocalForm = $<AppForm>("#setup-local-form");
  loginForm.addEventListener("submit", (event) => {
    event.preventDefault();
    completeAuthentication(
      "/api/auth/login",
      loginForm,
      requiredDescendant(loginForm, ".form-message"),
      { remember: loginForm.remember.checked },
    );
  });
  registerForm.addEventListener("submit", (event) => {
    event.preventDefault();
    completeAuthentication(
      "/api/auth/register",
      registerForm,
      requiredDescendant(registerForm, ".form-message"),
      { invitationToken },
    );
  });
  setupLocalForm.addEventListener("submit", (event) => {
    event.preventDefault();
    completeAuthentication(
      "/api/auth/register",
      setupLocalForm,
      requiredDescendant(setupLocalForm, ".form-message"),
    );
  });

  $("#setup-choose-plex").addEventListener("click", () => showSetupPanel("plex-login"));
  $("#setup-skip-plex").addEventListener("click", () => showSetupPanel("local-account"));
  document.querySelectorAll<HTMLElement>("[data-setup-back]").forEach((button) => {
    button.addEventListener("click", () => {
      const destination = button.dataset.setupBack;
      if (destination === "plex-login") showSetupPanel("plex-login");
      else showSetupPanel("choice");
    });
  });

  const setupPlexButton = $<HTMLButtonElement>("#setup-plex-login-button");
  const setupPlexMessage = $("#setup-plex-login .form-message");
  const setupPlexServerMessage =
    document.querySelector<HTMLElement>("#setup-plex-message")
    || document.querySelector<HTMLElement>("#setup-plex-server .form-message")
    || $<HTMLElement>("#setup-plex-server .message");
  setupPlexButton.addEventListener("click", async () => {
    setupPlexButton.disabled = true;
    await startPlexAuthentication("server", setupPlexMessage, async (result) => {
      setupPlexFlowToken = result.flowToken;
      setupPlexServers = result.servers;
      populatePlexServers("setup", setupPlexServers);
      $("#setup-plex-account").textContent =
        `Signed in as ${result.account.username || result.account.email}. Choose how Melodarr should reach your server.`;
      showSetupPanel("plex-server");
      setMessage(setupPlexServerMessage, "");
    });
    setupPlexButton.disabled = false;
  });

  const regularPlexButton = $<HTMLButtonElement>("#plex-login");
  const regularPlexMessage = $("#plex-login-option .form-message");
  regularPlexButton.addEventListener("click", async () => {
    regularPlexButton.disabled = true;
    await startPlexAuthentication("login", regularPlexMessage, async (result) => {
      window.history.replaceState({ view: "discover" }, "", "/");
      await applyCurrentUser(result as CurrentUser);
      if (result.role === "admin") await refreshSettings();
    });
    regularPlexButton.disabled = false;
  });

  const resetSetupPlexLibraries = () => {
    $("#setup-plex-libraries").disabled = true;
    $<HTMLButtonElement>("#setup-finish-plex").disabled = true;
  };
  plexControlElements("setup").server.addEventListener("change", () => {
    populatePlexConnections("setup", setupPlexServers);
    resetSetupPlexLibraries();
  });
  plexControlElements("setup").connection.addEventListener(
    "change",
    resetSetupPlexLibraries,
  );

  const inspectButton = $<HTMLButtonElement>("#setup-test-plex");
  inspectButton.addEventListener("click", async () => {
    inspectButton.disabled = true;
    setMessage(setupPlexServerMessage, "Connecting to Plex…");
    try {
      const result = await api("/api/auth/plex/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          flowToken: setupPlexFlowToken,
          ...selectedPlexConnection("setup"),
        }),
      });
      populateOptionPicker(
        $("#setup-plex-library-sections"),
        result.libraries,
        "title",
        "id",
        result.libraries.map((library: JsonObject) => library.id),
      );
      $("#setup-plex-libraries").disabled = false;
      $<HTMLButtonElement>("#setup-finish-plex").disabled = false;
      setMessage(setupPlexServerMessage, result.message);
    } catch (error) {
      $("#setup-plex-libraries").disabled = true;
      $<HTMLButtonElement>("#setup-finish-plex").disabled = true;
      setMessage(setupPlexServerMessage, error.message, true);
    } finally {
      inspectButton.disabled = false;
    }
  });

  const finishButton = $<HTMLButtonElement>("#setup-finish-plex");
  finishButton.addEventListener("click", async () => {
    const selectedIds = selectedOptionPickerValues("#setup-plex-library-sections");
    if (!selectedIds.length) {
      setMessage(
        setupPlexServerMessage,
        "Select at least one Plex music library.",
        true,
      );
      return;
    }
    finishButton.disabled = true;
    setMessage(
      setupPlexServerMessage,
      "Saving Plex and creating your administrator…",
    );
    try {
      const user = await api<CurrentUser>("/api/auth/plex/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          flowToken: setupPlexFlowToken,
          librarySectionIds: selectedIds,
        }),
      });
      window.history.replaceState({ view: "discover" }, "", "/");
      await applyCurrentUser(user);
      await refreshSettings();
    } catch (error) {
      setMessage(setupPlexServerMessage, error.message, true);
      finishButton.disabled = false;
    }
  });
  $("#logout").addEventListener("click", () => signOut());
}

/** End the session from either the header button or the account menu. */
async function signOut() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    window.dispatchEvent(new Event("melodarr-signed-out"));
    showAuth({ resetPath: true });
  }
}

function setupLidarrSettings() {
  const form = $<AppForm>("#lidarr-settings");
  const testButton = $("#test-lidarr");
  const message = requiredDescendant<HTMLElement>(form, ".form-message");

  testButton.addEventListener("click", async () => {
    const body: JsonObject = Object.fromEntries(new FormData(form));
    body.useSsl = form.useSsl.checked;
    testButton.disabled = true;
    setMessage(message, "Testing connection…");

    try {
      const result = await api("/api/settings/lidarr/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      populateLidarrOptions(result.options);
      $("#lidarr-defaults").disabled = false;
      setMessage(message, `${result.message} Choose defaults, then save.`);
    } catch (error) {
      $("#lidarr-defaults").disabled = true;
      setMessage(message, error.message, true);
    } finally {
      testButton.disabled = false;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = requiredDescendant<HTMLButtonElement>(form, "fieldset button");
    const body: JsonObject = Object.fromEntries(new FormData(form));
    body.useSsl = form.useSsl.checked;
    body.tags = selectedOptionPickerValues("#default-tags").map(Number);
    body.searchForMissingAlbums = form.searchForMissingAlbums.checked;
    submitButton.disabled = true;
    setMessage(message, "Saving service…");

    try {
      await api("/api/settings/lidarr", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setMessage(message, "Lidarr service saved.");
      await refreshSettings();
      window.dispatchEvent(new Event("melodarr-lidarr-settings-changed"));
    } catch (error) {
      setMessage(message, error.message, true);
    } finally {
      submitButton.disabled = false;
    }
  });
}

function setupPlexSettings() {
  const card = $("#plex-settings");
  const connectButton = $<HTMLButtonElement>("#connect-plex");
  const testButton = $<HTMLButtonElement>("#settings-test-plex");
  const saveButton = $<HTMLButtonElement>("#settings-save-plex");
  const message = requiredDescendant<HTMLElement>(card, ".form-message");

  plexControlElements("settings").server.addEventListener("change", () => {
    populatePlexConnections("settings", settingsPlexServers);
    $("#plex-libraries").disabled = true;
    saveButton.disabled = true;
  });
  plexControlElements("settings").connection.addEventListener("change", () => {
    $("#plex-libraries").disabled = true;
    saveButton.disabled = true;
  });

  connectButton.addEventListener("click", async () => {
    connectButton.disabled = true;
    await startPlexAuthentication("server", message, async (result) => {
      settingsPlexFlowToken = result.flowToken;
      settingsPlexServers = result.servers;
      populatePlexServers("settings", settingsPlexServers);
      $("#plex-current-connection").hidden = true;
      $("#plex-settings-config").hidden = false;
      $("#plex-libraries").disabled = true;
      saveButton.disabled = true;
      setMessage(
        message,
        `Signed in as ${result.account.username || result.account.email}. Choose a server connection.`,
      );
    });
    connectButton.disabled = false;
  });

  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    setMessage(message, "Connecting to Plex…");
    try {
      const result = await api("/api/auth/plex/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          flowToken: settingsPlexFlowToken,
          ...selectedPlexConnection("settings"),
        }),
      });
      populatePlexLibraries(
        result.libraries,
        result.libraries.map((library: JsonObject) => library.id),
      );
      saveButton.disabled = false;
      setMessage(message, result.message);
    } catch (error) {
      $("#plex-libraries").disabled = true;
      saveButton.disabled = true;
      setMessage(message, error.message, true);
    } finally {
      testButton.disabled = false;
    }
  });

  saveButton.addEventListener("click", async () => {
    const selectedIds = selectedOptionPickerValues("#plex-library-sections");
    if (!selectedIds.length) {
      setMessage(message, "Select at least one Plex music library.", true);
      return;
    }
    saveButton.disabled = true;
    setMessage(message, "Saving Plex libraries…");

    try {
      const user = await api<CurrentUser>("/api/auth/plex/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          flowToken: settingsPlexFlowToken,
          librarySectionIds: selectedIds,
        }),
      });
      currentUser = user;
      $("#plex-settings-config").hidden = true;
      setMessage(message, "Connected to Plex; full music-library scan queued.");
      await refreshSettings();
    } catch (error) {
      setMessage(message, error.message, true);
      saveButton.disabled = false;
    }
  });
}

function setupLibrary() {
  const results = $("#library-results");
  const search = $("#library-search") as HTMLInputElement;
  const filter = $("#library-filter");
  const filterCount = $("#library-filter-count");
  const loadButton = $<HTMLButtonElement>("#load-library");
  const renderBatchSize = 24;
  const maxArtworkRequests = 6;
  let libraryArtists: JsonObject[] = [];
  let visibleArtists: JsonObject[] = [];
  let renderedArtistCount = 0;
  let loadState: "idle" | "loading" | "loaded" | "error" = "idle";
  let renderVersion = 0;
  let filterFrame: number | undefined;
  const activeArtworkLoads = new Map<HTMLImageElement, number>();
  const artworkQueue: Array<{ image: HTMLImageElement; source: string }> = [];
  const deferredArtwork = new Map<Element, string>();

  const discardDetachedArtwork = () => {
    for (let index = artworkQueue.length - 1; index >= 0; index -= 1) {
      if (!artworkQueue[index].image.isConnected) artworkQueue.splice(index, 1);
    }
    activeArtworkLoads.forEach((guard, image) => {
      if (image.isConnected) return;
      window.clearTimeout(guard);
      activeArtworkLoads.delete(image);
      image.removeAttribute("src");
    });
  };

  const pumpArtworkQueue = () => {
    discardDetachedArtwork();
    while (activeArtworkLoads.size < maxArtworkRequests && artworkQueue.length) {
      const artwork = artworkQueue.shift()!;
      if (!artwork.image.isConnected) continue;
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        activeArtworkLoads.delete(artwork.image);
        window.clearTimeout(guard);
        pumpArtworkQueue();
      };
      const guard = window.setTimeout(finish, 45_000);
      activeArtworkLoads.set(artwork.image, guard);
      artwork.image.addEventListener("load", finish, { once: true });
      artwork.image.addEventListener("error", () => {
        artwork.image.remove();
        finish();
      }, { once: true });
      artwork.image.src = artwork.source;
    }
  };

  const artworkObserver = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          artworkObserver?.unobserve(entry.target);
          const source = deferredArtwork.get(entry.target);
          deferredArtwork.delete(entry.target);
          if (source) {
            artworkQueue.push({
              image: entry.target as HTMLImageElement,
              source,
            });
            pumpArtworkQueue();
          }
        });
      }, { rootMargin: "350px" })
    : null;

  const deferArtwork = (image: HTMLImageElement, source: string) => {
    if (artworkObserver) {
      deferredArtwork.set(image, source);
      artworkObserver.observe(image);
      return;
    }
    window.setTimeout(() => {
      artworkQueue.push({ image, source });
      pumpArtworkQueue();
    }, 0);
  };

  const createArtistCard = (artist: JsonObject) => {
    const card = document.createElement("div");
    card.className = `library-card${artist.musicbrainzId ? " clickable" : ""}`;
    card.dataset.search = artist.search;
    const artwork = document.createElement("div");
    artwork.className = "library-artwork";
    if (artist.artwork) {
      const image = document.createElement("img");
      image.alt = "";
      image.decoding = "async";
      image.fetchPriority = "low";
      image.width = 384;
      image.height = 384;
      // Do not assign src until the card is near the viewport. Large Plex
      // libraries otherwise start hundreds of authenticated artwork requests.
      const separator = String(artist.artwork).includes("?") ? "&" : "?";
      deferArtwork(image, `${artist.artwork}${separator}size=card`);
      artwork.append(image);
    }
    const info = document.createElement("div");
    info.className = "library-card-info";
    const name = document.createElement("strong");
    name.textContent = artist.name;
    const section = document.createElement("span");
    section.textContent = artist.musicbrainzId
      ? `${artist.section} · View discography`
      : `${artist.section} · MusicBrainz match unavailable`;
    info.append(name, section);
    card.append(artwork, info);
    if (artist.musicbrainzId) {
      card.tabIndex = 0;
      card.setAttribute("role", "link");
      const openArtist = () => window.dispatchEvent(new CustomEvent(
        "melodarr-open-detail",
        { detail: { kind: "artist", id: artist.musicbrainzId } },
      ));
      card.addEventListener("click", openArtist);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openArtist();
        }
      });
    }
    return card;
  };

  const createRenderSentinel = () => {
    const sentinel = document.createElement("div");
    sentinel.className = "library-render-sentinel";
    sentinel.setAttribute("aria-hidden", "true");
    return sentinel;
  };
  let renderSentinel = createRenderSentinel();

  let paginationObserver: IntersectionObserver | null = null;

  const renderArtists = (version: number) => {
    if (
      version !== renderVersion
      || !renderSentinel.isConnected
      || renderSentinel.parentElement !== results
    ) return;
    paginationObserver?.unobserve(renderSentinel);
    const fragment = document.createDocumentFragment();
    const end = Math.min(renderedArtistCount + renderBatchSize, visibleArtists.length);
    for (let index = renderedArtistCount; index < end; index += 1) {
      fragment.append(createArtistCard(visibleArtists[index]));
    }
    results.insertBefore(fragment, renderSentinel);
    renderedArtistCount = end;
    if (end < visibleArtists.length) {
      paginationObserver?.observe(renderSentinel);
    } else {
      renderSentinel.remove();
    }
  };

  const maybeRenderMore = () => {
    if (
      !paginationObserver
      && renderSentinel.isConnected
      && renderSentinel.getBoundingClientRect().top < window.innerHeight + 800
    ) {
      renderArtists(renderVersion);
      window.requestAnimationFrame(maybeRenderMore);
    }
  };

  if ("IntersectionObserver" in window) {
    paginationObserver = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting && entry.target === renderSentinel)) {
        renderArtists(renderVersion);
      }
    }, { rootMargin: "800px 0px" });
  }
  if (!paginationObserver) {
    window.addEventListener("scroll", maybeRenderMore, { passive: true });
    window.addEventListener("resize", maybeRenderMore);
  }

  const filterArtists = () => {
    const query = normalizeSearch(search.value);
    visibleArtists = query
      ? libraryArtists.filter((artist) => artist.search.includes(query))
      : libraryArtists;
    filterCount.textContent = query
      ? `${visibleArtists.length} of ${libraryArtists.length} artists`
      : `${libraryArtists.length} artists`;
    setMessage(
      $("#library-message"),
      query && !visibleArtists.length ? `No Plex artists match “${search.value.trim()}”.` : "",
    );

    // Filtering starts a new viewport-sized result set instead of walking or
    // retaining every card in a large Plex library.
    renderVersion += 1;
    renderedArtistCount = 0;
    paginationObserver?.disconnect();
    renderSentinel = createRenderSentinel();
    artworkObserver?.disconnect();
    deferredArtwork.clear();
    artworkQueue.length = 0;
    if (!visibleArtists.length) {
      results.replaceChildren();
      discardDetachedArtwork();
      return;
    }
    results.replaceChildren(renderSentinel);
    discardDetachedArtwork();
    renderArtists(renderVersion);
    window.requestAnimationFrame(maybeRenderMore);
  };

  search.addEventListener("input", () => {
    if (filterFrame !== undefined) return;
    filterFrame = window.requestAnimationFrame(() => {
      filterFrame = undefined;
      filterArtists();
    });
  });

  const loadLibrary = async (force = false) => {
    if (loadState === "loading" || (!force && loadState !== "idle")) return;
    loadState = "loading";
    if (filterFrame !== undefined) {
      window.cancelAnimationFrame(filterFrame);
      filterFrame = undefined;
    }
    loadButton.disabled = true;
    loadButton.textContent = "Loading…";
    results.setAttribute("aria-busy", "true");
    artworkObserver?.disconnect();
    deferredArtwork.clear();
    artworkQueue.length = 0;
    paginationObserver?.disconnect();
    results.replaceChildren(skeletonBlock("library-card", 12));
    discardDetachedArtwork();
    renderVersion += 1;
    libraryArtists = [];
    visibleArtists = [];
    renderedArtistCount = 0;
    search.value = "";
    filter.hidden = true;
    setMessage($("#library-message"), "Loading Plex library…");

    try {
      const library = await api("/api/library");
      results.replaceChildren();
      libraryArtists = library.artists.map((artist: JsonObject) => ({
        ...artist,
        search: normalizeSearch(
          [artist.name, artist.sortName].filter(Boolean).join(" "),
        ),
      }));
      $("#library-copy").textContent = `${library.artistCount} artists and ${library.releaseGroupCount} releases available in your Plex music libraries.`;
      setMessage($("#library-message"), "");
      filter.hidden = false;
      filterArtists();
      loadState = "loaded";
    } catch (error) {
      results.replaceChildren();
      loadState = "error";
      loadButton.textContent = "Retry";
      setMessage($("#library-message"), `We couldn’t load your Plex library. ${error.message}`, true);
    } finally {
      results.removeAttribute("aria-busy");
      loadButton.disabled = false;
      if (loadState === "loaded") loadButton.textContent = "Reload";
    }
  };

  loadButton.addEventListener("click", () => loadLibrary(true));
  window.addEventListener("melodarr-library-visible", () => loadLibrary());
}

setupTheme();
setupNavigation();
setupAdminRequests();
setupAdminUsers();
setupStandalonePullToRefresh();
setupLidarrSettings();
setupPlexSettings();
setupLibrary();
setupAuth();

api<CurrentUser>("/api/auth/me")
  .then(async (user) => {
    if (["/setup", "/register"].includes(window.location.pathname)) {
      window.history.replaceState({ view: "discover" }, "", "/");
    }
    await applyCurrentUser(user);
    if (user.role === "admin") await refreshSettings(window.location.pathname === "/settings");
  })
  .catch(() => showAuth());
