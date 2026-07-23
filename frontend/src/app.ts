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
type AccountPage = "profile" | "general" | "linked-accounts" | "invitations";
type AppView = "discover" | "detail" | "library" | "settings" | "account";
type SettingsPage = "services" | "jobs";

interface CurrentUser {
  username: string;
  role: "admin" | "user";
  csrfToken?: string;
  listenbrainzUsername?: string;
  lastfmUsername?: string;
  lastfmConfigured?: boolean;
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
let showAccountPage: ((page?: AccountPage, updateHistory?: boolean) => void) | undefined;
let invitationToken = "";
let maintenanceRefreshTimer: number | undefined;
let maintenanceRefreshInFlight = false;
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
  element.className = `message${isError ? " error" : ""}`;
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
      ? "The server returned an invalid response."
      : `Request failed with status ${response.status}.`;
    throw new Error(message);
  }

  if (!response.ok) {
    throw new Error(body.error || "Request failed.");
  }
  return body;
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
  $("#plex-state").textContent = plex.configured ? `Connected · ${plex.url}` : "Not connected";
  const plexForm = $<AppForm>("#plex-settings");
  plexForm.url.value = plex.url || "";
  plexForm.token.value = "";
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
  $("#settings-jobs").hidden = page !== "jobs";
  if (maintenanceRefreshTimer !== undefined) window.clearInterval(maintenanceRefreshTimer);
  maintenanceRefreshTimer = undefined;
  if (page === "jobs") {
    refreshMaintenance();
    maintenanceRefreshTimer = window.setInterval(refreshMaintenance, 10_000);
  }
  if (updateHistory) {
    window.history.pushState({ view: "settings", settings: page }, "", page === "jobs" ? "/settings/jobs" : "/settings");
    resetPageScroll();
  }
}

function setupNavigation() {
  function showView(view: AppView, updateHistory = true) {
    if (!currentUser || (currentUser.role !== "admin" && !VIEWS_FOR_EVERY_USER.includes(view))) view = "discover";
    document.querySelectorAll(".nav-link, .view").forEach((element) => element.classList.remove("active"));
    // Account and detail are application views without a matching nav button,
    // and the header and bottom tab bar both carry a button per view, so this
    // marks every match rather than using the strict single-element helper.
    document.querySelectorAll<HTMLElement>(`[data-view="${view}"]`).forEach((button) => button.classList.add("active"));
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

  function accountPath(page: AccountPage) {
    if (!currentUser) throw new Error("Account navigation requires an authenticated user.");
    const username = encodeURIComponent(currentUser.username);
    return page === "profile" ? `/${username}` : `/${username}/settings/${page}`;
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
    const content = $("#account-content");
    $("#account-title").textContent = page === "profile" ? "Profile" : page.split("-").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ");
    document.querySelectorAll<HTMLElement>("[data-account-route]").forEach((link) => link.classList.toggle("active", link.dataset.accountRoute === page));
    content.replaceChildren();
    const message = document.createElement("p");
    message.className = "message";
    message.textContent = "Loading…";
    content.append(message);
    try {
      if (page === "profile") {
        const data = await api("/api/account/profile");
        content.replaceChildren();
        [["Artists", data.requests.artist, "artists"], ["Release groups", data.requests["release-group"], "albums"]].forEach(([title, requests, route]) => {
          const section = document.createElement("section");
          section.className = "account-section";
          const heading = document.createElement("h2"); heading.textContent = title;
          const list = document.createElement("div"); list.className = "results";
          if (!requests.length) { const empty = document.createElement("p"); empty.className = "message"; empty.textContent = "No requests yet."; list.append(empty); }
          requests.forEach((item: JsonObject) => list.append(createHistoryItem(item, route)));
          section.append(heading, list); content.append(section);
        });
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
        content.replaceChildren();
        const form = document.createElement("form") as AppForm; form.className = "service-card account-form";
        form.innerHTML = '<h2>Linked accounts</h2><fieldset><legend>ListenBrainz</legend><label>Username<small>Used to tailor recommendations. Leave blank to disconnect it.</small><input name="listenbrainzUsername" autocomplete="username" placeholder="your-listenbrainz-name"></label></fieldset><fieldset><legend>Last.fm</legend><label>Username<input name="lastfmUsername" autocomplete="username" placeholder="your-lastfm-name"></label><label>API key<small>Create one in your Last.fm API account. Leave blank to keep your saved key.</small><input name="lastfmApiKey" type="password" autocomplete="off" placeholder="Last.fm API key"></label></fieldset><div class="form-actions"><p class="form-message"></p><button>Save linked accounts</button></div>';
        form.listenbrainzUsername.value = user.listenbrainzUsername || "";
        form.lastfmUsername.value = user.lastfmUsername || "";
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

  showAccountPage = (page = "profile", updateHistory = true) => {
    if (!currentUser) return;
    showView("account", false);
    if (updateHistory) window.history.pushState({ account: page }, "", accountPath(page));
    const allowedPages = ["profile", "general", "linked-accounts"];
    if (currentUser.role === "admin") allowedPages.push("invitations");
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
    const accountMatch = window.location.pathname.match(/^\/([^/]+)(?:\/settings\/(general|linked-accounts|invitations))?\/?$/);
    if (accountMatch && currentUser && decodeURIComponent(accountMatch[1]).toLowerCase() === currentUser.username.toLowerCase()) {
      showAccountPage?.((accountMatch[2] || "profile") as AccountPage, false);
      return;
    }
    if (window.location.pathname === "/settings" || window.location.pathname === "/settings/jobs") {
      showView("settings", false);
      showSettingsPage(window.location.pathname.endsWith("/jobs") ? "jobs" : "services", false);
      return;
    }
    const view = window.location.pathname.slice(1) || "discover";
    showView((["discover", "library", "settings"].includes(view) ? view : "discover") as AppView, false);
  });

  const initialView = window.location.pathname.slice(1) || "discover";
  if (initialView === "settings/jobs") showView("settings", false);
  else if (["library", "settings"].includes(initialView)) showView(initialView as AppView, false);

  document.querySelectorAll<HTMLElement>(".tab-bar .nav-link").forEach((button) => button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }));

  $<HTMLAnchorElement>("#account-menu").addEventListener("click", (event) => {
    if (!currentUser || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showAccountPage?.("profile");
  });
  document.querySelectorAll<HTMLElement>("[data-account-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); showAccountPage?.(link.dataset.accountRoute as AccountPage); }));
  document.querySelector("#account-logout")?.addEventListener("click", () => signOut());
  document.querySelectorAll<HTMLElement>("[data-settings-page]").forEach((button) => button.addEventListener("click", () => showSettingsPage(button.dataset.settingsPage as SettingsPage)));
  document.querySelector("#refresh-maintenance")?.addEventListener("click", () => refreshMaintenance());
}

function applyCurrentUser(user: CurrentUser) {
  currentUser = user;
  document.body.classList.add("authenticated");
  const isAdmin = user.role === "admin";
  document.querySelectorAll<HTMLElement>(".admin-only").forEach((element) => { element.hidden = !isAdmin; });
  const status = $("#status");
  status.textContent = `Signed in as ${user.username}${isAdmin ? " · Administrator" : ""}`;
  status.className = "status ready";
  const accountMenu = $<HTMLAnchorElement>("#account-menu");
  accountMenu.textContent = user.username.slice(0, 1).toUpperCase();
  accountMenu.href = `/${encodeURIComponent(user.username)}`;
  accountMenu.setAttribute("aria-label", `Open settings for ${user.username}`);
  window.dispatchEvent(new Event("melodarr-authenticated"));
  // Re-evaluate a bookmarked view or detail route only after its API calls
  // have an authenticated session.
  window.dispatchEvent(new PopStateEvent("popstate"));
}

async function showAuth({ resetPath = false } = {}) {
  currentUser = undefined;
  document.body.classList.remove("authenticated");
  const loginForm = $("#login-form");
  const registerForm = $("#register-form");
  loginForm.hidden = true;
  registerForm.hidden = true;
  if (resetPath) window.history.replaceState({}, "", "/");

  const parameters = new URLSearchParams(window.location.search);
  invitationToken = parameters.get("invite") || "";
  try {
    const query = invitationToken ? `?invite=${encodeURIComponent(invitationToken)}` : "";
    const status = await api(`/api/auth/status${query}`);
    if (status.firstAccount) {
      invitationToken = "";
      window.history.replaceState({ setup: true }, "", "/setup");
      $("#auth-title").innerHTML = "Create your<br><em>owner account.</em>";
      $("#auth-intro").textContent = "Set up the first Melodarr administrator account.";
      $("#register-title").textContent = "Create owner account";
      registerForm.hidden = false;
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
    applyCurrentUser(user);
    form.reset();
    if (user.role === "admin") await refreshSettings(window.location.pathname.startsWith("/settings"));
  } catch (error) {
    setMessage(message, error.message, true);
  }
}

function setupAuth() {
  const loginForm = $<AppForm>("#login-form");
  const registerForm = $<AppForm>("#register-form");
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
  $("#logout").addEventListener("click", () => signOut());
}

/** End the session from either the header button or the account menu. */
async function signOut() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
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
  const form = $<AppForm>("#plex-settings");
  const testButton = $("#test-plex");
  const message = requiredDescendant<HTMLElement>(form, ".form-message");

  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    setMessage(message, "Testing Plex connection…");
    try {
      const result = await api("/api/settings/plex/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      const current = selectedOptionPickerValues("#plex-library-sections");
      populatePlexLibraries(result.libraries, current.length ? current : result.libraries.map((library: JsonObject) => library.id));
      setMessage(message, result.message);
    } catch (error) {
      $("#plex-libraries").disabled = true;
      setMessage(message, error.message, true);
    } finally {
      testButton.disabled = false;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = requiredDescendant<HTMLButtonElement>(form, "fieldset button");
    const selectedIds = selectedOptionPickerValues("#plex-library-sections");
    if (!selectedIds.length) {
      setMessage(message, "Select at least one Plex music library.", true);
      return;
    }
    const body: JsonObject = Object.fromEntries(new FormData(form));
    body.librarySectionIds = selectedIds;
    submitButton.disabled = true;
    setMessage(message, "Saving Plex libraries…");

    try {
      const result = await api("/api/settings/plex", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      populatePlexLibraries(result.libraries, result.librarySectionIds);
      requiredDescendant<HTMLInputElement>(form, "[name=token]").value = "";
      setMessage(message, result.message);
      await refreshSettings();
    } catch (error) {
      setMessage(message, error.message, true);
    } finally {
      submitButton.disabled = false;
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
      setMessage($("#library-message"), error.message, true);
    } finally {
      loadButton.disabled = false;
      if (loadState === "loaded") loadButton.textContent = "Reload";
    }
  };

  loadButton.addEventListener("click", () => loadLibrary(true));
  window.addEventListener("melodarr-library-visible", () => loadLibrary());
}

setupNavigation();
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
    applyCurrentUser(user);
    if (user.role === "admin") await refreshSettings(window.location.pathname === "/settings");
  })
  .catch(() => showAuth());
