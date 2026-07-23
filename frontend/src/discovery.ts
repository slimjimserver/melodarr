(() => {
  type DetailKind = "artist" | "release-group" | "release";
  type DetailReference = { kind: DetailKind; id: string };
  type DetailOrigin = { view: "discover" | "library"; scrollY: number };
  type ArtworkItem = { image: HTMLImageElement; source: string; fallback: HTMLElement };
  type ArtworkJob = { guard: ReturnType<typeof setTimeout> };
  type DetailRequest = {
    prefetch: boolean;
    settled: boolean;
    promise: Promise<JsonObject>;
    expiresAt: number;
    lastAccessedAt: number;
  };
  type ArtistRevalidation = {
    artistId: string;
    baselineRefreshAt: number;
    expiresAt: number;
    timer?: ReturnType<typeof setTimeout>;
  };

  const $ = <T extends Element = AppElement>(selector: string): T => {
    const element = document.querySelector<T>(selector);
    if (!element) throw new Error(`Required element not found: ${selector}`);
    return element;
  };
  let currentDetail: DetailReference | null = null;
  const detailHistory: DetailReference[] = [];
  let detailOrigin: DetailOrigin = { view: "discover", scrollY: 0 };
  let requestedArtist: JsonObject | undefined;
  let lidarrExternalUrl: string | undefined;
  let recommendationPoll: ReturnType<typeof setTimeout> | undefined;
  let searchRequestVersion = 0;
  let searchDebounce: ReturnType<typeof setTimeout>;
  let searchAbort: AbortController | undefined;
  const detailRequests = new Map<string, DetailRequest>();
  const detailUpgrades = new Map<string, Promise<JsonObject>>();
  let artistRevalidation: ArtistRevalidation | undefined;
  const detailCacheMaxEntries = 32;
  const detailPrefetchTtl = 2 * 60 * 1000;
  const detailOpenedTtl = 15 * 60 * 1000;
  const artworkQueue: ArtworkItem[] = [];
  const deferredArtwork = new WeakMap<Element, Omit<ArtworkItem, "image">>();
  const activeArtworkLoads = new Map<HTMLImageElement, ArtworkJob>();
  // Melodarr now serves downscaled variants rather than the provider's
  // full-size originals, so more covers can be in flight without a single view
  // occupying every web-request thread.
  const maxArtworkRequests = 6;

  const normalizeSearch = (value: string) => value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .trim();

  function discardDetachedArtwork() {
    for (let index = artworkQueue.length - 1; index >= 0; index -= 1) {
      if (!artworkQueue[index].image.isConnected) artworkQueue.splice(index, 1);
    }
    activeArtworkLoads.forEach((job, image) => {
      if (image.isConnected) return;
      clearTimeout(job.guard);
      activeArtworkLoads.delete(image);
      image.removeAttribute("src");
    });
  }

  function pumpArtworkQueue() {
    discardDetachedArtwork();
    while (activeArtworkLoads.size < maxArtworkRequests && artworkQueue.length) {
      const { image, source, fallback } = artworkQueue.shift()!;
      if (!image.isConnected) continue;
      const job = {} as ArtworkJob;
      const finished = () => {
        if (activeArtworkLoads.get(image) !== job) return;
        clearTimeout(job.guard);
        activeArtworkLoads.delete(image);
        pumpArtworkQueue();
      };
      job.guard = setTimeout(finished, 45_000);
      activeArtworkLoads.set(image, job);
      image.addEventListener("load", finished, { once: true });
      image.addEventListener("error", () => {
        if (fallback && image.isConnected) image.replaceWith(fallback);
        finished();
      }, { once: true });
      // The queue already controls when an image starts, so native lazy
      // loading must not defer it again after a view is hidden and restored.
      image.loading = "eager";
      image.src = source;
    }
  }

  const artworkObserver = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          artworkObserver?.unobserve(entry.target);
          const artwork = deferredArtwork.get(entry.target);
          deferredArtwork.delete(entry.target);
          if (artwork) {
            artworkQueue.push({ image: entry.target as HTMLImageElement, ...artwork });
            pumpArtworkQueue();
          }
        });
      }, { rootMargin: "300px" })
    : null;

  function loadArtworkWhenNear(image: HTMLImageElement, source: string, fallback: HTMLElement) {
    deferredArtwork.set(image, { source, fallback });
    if (artworkObserver) {
      artworkObserver.observe(image);
    } else {
      setTimeout(() => {
        artworkQueue.push({ image, source, fallback });
        pumpArtworkQueue();
      }, 0);
    }
  }

  new MutationObserver((records) => {
    if (!records.some((record) => record.removedNodes.length)) return;
    artworkObserver && records.forEach((record) => record.removedNodes.forEach((node) => {
      if (!(node instanceof Element)) return;
      if (node.matches("img") && !node.isConnected) {
        artworkObserver.unobserve(node);
        deferredArtwork.delete(node);
      }
      node.querySelectorAll("img").forEach((image) => {
        if (image.isConnected) return;
        artworkObserver.unobserve(image);
        deferredArtwork.delete(image);
      });
    }));
    pumpArtworkQueue();
  }).observe(document.body, { childList: true, subtree: true });

  async function getJson(
    url: string,
    timeoutMilliseconds = 30_000,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMilliseconds);
    // A superseded typeahead query aborts through the caller's signal, while
    // the timeout above still bounds every request.
    signal?.addEventListener("abort", () => controller.abort(), { once: true });

    try {
      const response = await fetch(url, { signal: controller.signal });
      if (response.status === 204) {
        const error = new Error("Requested detail is not cached.");
        error.name = "CacheMissError";
        throw error;
      }
      const body = await response.json() as JsonObject;
      if (!response.ok) throw new Error(body.error || "MusicBrainz request failed.");
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function getLidarrExternalUrl() {
    if (lidarrExternalUrl !== undefined) return lidarrExternalUrl;
    const settings = await getJson("/api/settings");
    lidarrExternalUrl = settings.lidarr.externalUrl || "";
    return lidarrExternalUrl;
  }

  async function postJson(url: string, body: JsonObject): Promise<JsonObject> {
    const headers = new Headers({ "Content-Type": "application/json" });
    if (currentUser?.csrfToken) headers.set("X-CSRF-Token", currentUser.csrfToken);
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed.");
    return result;
  }

  function showView(id: AppView) {
    if (id !== "detail") stopArtistRevalidation();
    document.querySelectorAll(".view, .nav-link").forEach((element) => element.classList.remove("active"));
    $(`#${id}`).classList.add("active");
    if (id !== "detail") {
      document.querySelectorAll<HTMLElement>(`[data-view="${id}"]`)
        .forEach((button) => button.classList.add("active"));
    }
    resetPageScroll();
  }

  function createCard(title: string, description: string, onClick?: EventListener, coverArt = "", detailKind?: DetailKind, detailId = "") {
    const card = document.createElement("article");
    card.className = `artist-card${onClick ? " clickable" : ""}`;
    const fallbackAvatar = document.createElement("div");
    fallbackAvatar.className = "avatar";
    let artwork: HTMLElement = fallbackAvatar;
    if (coverArt) {
      const image = document.createElement("img");
      image.className = "cover-art";
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.fetchPriority = "low";
      image.width = 44;
      image.height = 44;
      loadArtworkWhenNear(image, coverArt, fallbackAvatar);
      artwork = image;
    }
    const info = document.createElement("div");
    info.className = "artist-info";
    const heading = document.createElement("h2");
    heading.textContent = title;
    const text = document.createElement("p");
    text.textContent = description;
    info.append(heading, text);
    card.append(artwork, info);
    if (onClick) card.addEventListener("click", onClick);
    if (detailKind && detailId) addDetailPrefetch(card, detailKind, detailId);
    return card;
  }

  function artistDisplayName(artist: JsonObject) {
    const name = String(artist.name || "Unknown artist");
    const romanizedName = String(artist.romanizedName || "").trim();
    return romanizedName ? `${name} (${romanizedName})` : name;
  }

  function releaseGroupDisplayTitle(group: JsonObject) {
    const title = String(group.title || group.name || "Untitled release");
    const romanizedTitle = String(group.romanizedTitle || "").trim();
    return romanizedTitle ? `${title} (${romanizedTitle})` : title;
  }

  function addPlexAvailability(element: HTMLElement, label = "Available in Plex") {
    const badge = document.createElement("span");
    badge.className = "plex-availability";
    badge.textContent = label;
    element.append(badge);
  }

  function detailPath(kind: DetailKind, id: string) {
    const route: Record<DetailKind, string> = { artist: "artists", "release-group": "albums", release: "releases" };
    return `/${route[kind]}/${encodeURIComponent(id)}`;
  }

  function detailNavigationState(kind: DetailKind, id: string) {
    return {
      kind,
      id,
      detailOrigin,
      detailHistory: [...detailHistory],
    };
  }

  function resetDetailCover(showSkeleton = false) {
    const cover = $("#detail-cover");
    const image = $("#detail-cover-image");
    image.onload = null;
    image.onerror = null;
    image.hidden = true;
    image.removeAttribute("src");
    image.alt = "";
    cover.hidden = !showSkeleton;
    cover.classList.toggle("skeleton", showSkeleton);
  }

  function loadDetailCover(source: string, alt: string) {
    const cover = $("#detail-cover");
    const image = $("#detail-cover-image");
    resetDetailCover(true);
    image.fetchPriority = "high";
    image.onload = () => {
      image.hidden = false;
      cover.classList.remove("skeleton");
    };
    image.onerror = () => resetDetailCover();
    image.alt = alt;
    image.src = source;
  }

  function pruneDetailRequests(now = Date.now()) {
    detailRequests.forEach((entry, key) => {
      if (entry.settled && entry.expiresAt <= now) detailRequests.delete(key);
    });

    const settledEntries = [...detailRequests.entries()]
      .filter(([, entry]) => entry.settled)
      .sort((left, right) => left[1].lastAccessedAt - right[1].lastAccessedAt);
    settledEntries
      .slice(0, Math.max(0, settledEntries.length - detailCacheMaxEntries))
      .forEach(([key]) => detailRequests.delete(key));
  }

  function storeSettledDetail(key: string, data: JsonObject, prefetch = false) {
    const now = Date.now();
    detailRequests.set(key, {
      prefetch,
      settled: true,
      promise: Promise.resolve(data),
      expiresAt: now + (prefetch ? detailPrefetchTtl : detailOpenedTtl),
      lastAccessedAt: now,
    });
    pruneDetailRequests(now);
  }

  function loadDetail(kind: DetailKind, id: string, prefetch = false): Promise<JsonObject> {
    const key = `${kind}:${id}`;
    const now = Date.now();
    pruneDetailRequests(now);
    const existing = detailRequests.get(key);
    if (existing && (prefetch || !existing.prefetch || existing.settled)) {
      existing.lastAccessedAt = now;
      if (!prefetch) {
        existing.prefetch = false;
        if (existing.settled) existing.expiresAt = now + detailOpenedTtl;
      }
      return existing.promise;
    }

    const entry = {
      prefetch,
      settled: false,
      expiresAt: Number.POSITIVE_INFINITY,
      lastAccessedAt: now,
    } as DetailRequest;
    const query = prefetch ? "?prefetch=1" : "";
    const timeout = prefetch ? 30_000 : kind === "artist" ? 120_000 : 60_000;
    entry.promise = getJson(
      `/api/music/${kind}/${encodeURIComponent(id)}${query}`,
      timeout,
    )
      .then((data) => {
        entry.settled = true;
        const settledAt = Date.now();
        entry.expiresAt = settledAt + (entry.prefetch ? detailPrefetchTtl : detailOpenedTtl);
        entry.lastAccessedAt = settledAt;
        pruneDetailRequests(settledAt);
        return data;
      })
        .catch((error) => {
          if (detailRequests.get(key) === entry) detailRequests.delete(key);
          throw error;
        });
    detailRequests.set(key, entry);
    return entry.promise!;
  }

  function addDetailPrefetch(element: Element, kind: DetailKind, id: string) {
    let timer: ReturnType<typeof setTimeout>;
    element.addEventListener("pointerenter", () => {
      timer = setTimeout(() => loadDetail(kind, id, true).catch(() => {}), 200);
    });
    element.addEventListener("pointerleave", () => clearTimeout(timer));
    element.addEventListener("focus", () => loadDetail(kind, id, true).catch(() => {}));
  }

  function upgradeProvisionalDetail(kind: "artist" | "release-group", id: string) {
    const key = `${kind}:${id}`;
    if (detailUpgrades.has(key)) return;
    const upgrade = getJson(
      `/api/music/${kind}/${encodeURIComponent(id)}?complete=1`,
      120_000,
    );
    detailUpgrades.set(key, upgrade);
    upgrade
      .then((data) => {
        storeSettledDetail(key, data);
        if (currentDetail?.kind !== kind || currentDetail.id !== id) return;
        renderDetail(kind, data);
        $("#detail-message").textContent = kind === "artist"
          ? "Complete discography loaded from MusicBrainz."
          : "Complete release information loaded from MusicBrainz.";
      })
      .catch((error) => {
        if (currentDetail?.kind !== kind || currentDetail.id !== id) return;
        $("#detail-message").textContent = error.name === "AbortError"
          ? "The Lidarr metadata is shown. MusicBrainz is still taking too long to complete this page."
          : `The Lidarr metadata is shown. MusicBrainz enrichment failed: ${error.message}`;
      })
      .finally(() => detailUpgrades.delete(key));
  }

  function stopArtistRevalidation() {
    if (artistRevalidation?.timer) clearTimeout(artistRevalidation.timer);
    artistRevalidation = undefined;
  }

  function scheduleArtistRevalidationPoll(watcher: ArtistRevalidation) {
    if (Date.now() >= watcher.expiresAt) {
      if (artistRevalidation === watcher) artistRevalidation = undefined;
      if (currentDetail?.kind === "artist" && currentDetail.id === watcher.artistId) {
        $("#detail-message").textContent = "The cached discography remains available while its background update continues.";
      }
      return;
    }
    watcher.timer = setTimeout(() => pollArtistRevalidation(watcher), 1_500);
  }

  async function reloadRevalidatedArtist(watcher: ArtistRevalidation, refreshedAt: number) {
    const key = `artist:${watcher.artistId}`;
    detailRequests.delete(key);
    try {
      const data = await getJson(
        `/api/music/artist/${encodeURIComponent(watcher.artistId)}`
          + `?complete=1&revision=${encodeURIComponent(refreshedAt)}`,
        120_000,
      );
      storeSettledDetail(key, data);
      if (currentDetail?.kind !== "artist" || currentDetail.id !== watcher.artistId) return;
      renderDetail("artist", data);
      $("#detail-message").textContent = "Discography and artist metadata updated from MusicBrainz.";
    } catch (error) {
      if (currentDetail?.kind === "artist" && currentDetail.id === watcher.artistId) {
        $("#detail-message").textContent = `The cached discography is still shown: ${error.message}`;
      }
    }
  }

  async function pollArtistRevalidation(watcher: ArtistRevalidation) {
    if (
      artistRevalidation !== watcher
      || currentDetail?.kind !== "artist"
      || currentDetail.id !== watcher.artistId
    ) return;
    try {
      const state = await getJson(
        `/api/music/artist/${encodeURIComponent(watcher.artistId)}/revalidation`,
      );
      if (artistRevalidation !== watcher) return;
      if (state.status === "refreshing") {
        $("#detail-message").textContent = "MusicBrainz found a discography change; updating metadata…";
      }
      if (state.polling) {
        scheduleArtistRevalidationPoll(watcher);
        return;
      }
      artistRevalidation = undefined;
      const refreshedAt = Number(state.lastRefreshAt || 0);
      if (state.status === "refreshed" && refreshedAt > watcher.baselineRefreshAt) {
        await reloadRevalidatedArtist(watcher, refreshedAt);
      } else if (
        state.status === "failed"
        && currentDetail?.kind === "artist"
        && currentDetail.id === watcher.artistId
      ) {
        $("#detail-message").textContent = "The cached discography is shown; its background update will retry later.";
      }
    } catch {
      if (artistRevalidation === watcher) artistRevalidation = undefined;
    }
  }

  async function startArtistRevalidation(data: JsonObject) {
    if (
      data.provisional
      || data.metadataSource !== "MusicBrainz"
      || currentDetail?.kind !== "artist"
      || currentDetail.id !== data.id
      || artistRevalidation?.artistId === data.id
    ) return;
    try {
      const state = await postJson(
        `/api/music/artist/${encodeURIComponent(data.id)}/revalidate`,
        {},
      );
      if (
        !state.polling
        || currentDetail?.kind !== "artist"
        || currentDetail.id !== data.id
      ) return;
      const watcher: ArtistRevalidation = {
        artistId: data.id,
        baselineRefreshAt: Number(state.lastRefreshAt || 0),
        expiresAt: Date.now() + 5 * 60 * 1000,
      };
      artistRevalidation = watcher;
      scheduleArtistRevalidationPoll(watcher);
    } catch {
      // Opportunistic freshness checks must never disrupt a cached page.
    }
  }

  function showDetail(kind: DetailKind, id: string, addToHistory = true, updateHistory = true) {
    stopArtistRevalidation();
    const activeView = document.querySelector<HTMLElement>(".view.active")?.id;
    if (addToHistory && currentDetail && activeView === "detail") {
      detailHistory.push(currentDetail);
    } else if (addToHistory) {
      detailHistory.length = 0;
      detailOrigin = {
        view: activeView === "library" ? "library" : "discover",
        scrollY: window.scrollY,
      };
    }
    currentDetail = { kind, id };
    if (updateHistory) {
      window.history.pushState(
        detailNavigationState(kind, id),
        "",
        detailPath(kind, id),
      );
    }
    const previous = detailHistory.at(-1);
    $("#back-to-search").textContent = previous
      ? `← Back to ${previous.kind === "artist" ? "artist" : previous.kind === "release-group" ? "album" : "release"}`
      : detailOrigin.view === "library" ? "← Back to library" : "← Back to search";
    showView("detail");
    $("#detail-results").replaceChildren(skeletonBlock("skeleton-card", kind === "release" ? 6 : 4));
    $("#detail-title").textContent = "";
    $("#detail-eyebrow").textContent = "";
    $("#detail-subtitle").textContent = "";
    resetDetailCover(kind !== "release");
    $("#detail-message").textContent = kind === "artist"
      ? "Loading artist and discography…"
      : kind === "release-group" ? "Loading album and release information…" : "Loading release…";

    loadDetail(kind, id)
      .then((data) => {
        if (currentDetail?.kind !== kind || currentDetail?.id !== id) return;
        renderDetail(kind, data);
        if ((kind === "artist" || kind === "release-group") && data.provisional) {
          $("#detail-message").textContent = kind === "artist"
            ? "Showing Lidarr's catalog while MusicBrainz completes the discography…"
            : "Showing Lidarr's album metadata while MusicBrainz loads every release…";
          upgradeProvisionalDetail(kind, id);
        }
      })
      .catch((error) => {
        if (currentDetail?.kind !== kind || currentDetail?.id !== id) return;
        $("#detail-message").textContent = error.name === "AbortError"
          ? "MusicBrainz took too long to respond."
          : `Could not load this page: ${error.message}`;
        const retry = document.createElement("button");
        retry.className = "outline";
        retry.type = "button";
        retry.textContent = kind === "artist" ? "Retry discography" : "Retry";
        retry.addEventListener("click", () => showDetail(kind, id, false, false));
        $("#detail-results").replaceChildren(retry);
      });
  }

  function createServiceIconLink(url: string, icon: string, label: string, className = "", openInNewTab = true) {
    const link = document.createElement("a");
    link.className = className;
    link.href = url;
    link.title = label;
    link.setAttribute("aria-label", label);
    if (openInNewTab) {
      link.target = "_blank";
      link.rel = "noreferrer";
    }
    link.addEventListener("click", (event) => event.stopPropagation());
    const image = document.createElement("img");
    image.src = icon;
    image.alt = "";
    link.append(image);
    return link;
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

  function plexampArtistUrl(plexArtist: JsonObject) {
    if (plexArtist.plexampUrl) return plexArtist.plexampUrl;
    const plexGuid = [plexArtist.plexGuid, ...(plexArtist.guids || [])]
      .find((guid) => /^plex:\/\/artist\//i.test(String(guid || "")));
    const guidMatch = String(plexGuid || "").match(/^plex:\/\/artist\/(.+)$/i);
    const webMatch = String(plexArtist.url || "").match(/#!\/server\/([^/]+)\/details\?(.+)$/);
    if (!guidMatch || !webMatch) return "";
    const key = new URLSearchParams(webMatch[2]).get("key") || plexArtist.key;
    if (!key) return "";
    const url = new URL(`https://listen.plex.tv/artist/${encodeURIComponent(guidMatch[1])}`);
    url.searchParams.set("source", webMatch[1]);
    url.searchParams.set("key", key);
    return url.href;
  }

  function addExternalLinks(container: Element, kind: DetailKind, id: string, spotify?: string, plexUrl = "", plexampUrl = "") {
    const links = document.createElement("div");
    links.className = "external-icons";
    const destinations = [
      ["/icons/musicbrainz.svg", `https://musicbrainz.org/${kind}/${id}`, "Open on MusicBrainz"],
    ];

    destinations.forEach(([icon, url, label]) => links.append(
      createServiceIconLink(url, icon, label),
    ));
    if (spotify) {
      const mobile = isMobileDevice();
      links.append(createServiceIconLink(
        spotify,
        "/icons/spotify.svg",
        mobile ? "Open in Spotify" : "Open on Spotify",
        "",
        !mobile,
      ));
    }
    if (plexUrl) {
      const destination = mobilePlexDestination(plexUrl, plexampUrl);
      links.append(createServiceIconLink(
        destination.url,
        "/icons/plex.svg",
        destination.label,
        "",
        destination.openInNewTab,
      ));
    }
    container.append(links);
    getLidarrExternalUrl().then((externalUrl) => {
      if (!externalUrl) return;
      const resource = kind === "artist" ? "artist" : "album";
      links.append(createServiceIconLink(
        `${externalUrl}/${resource}/${encodeURIComponent(id)}`,
        "/icons/lidarr.svg",
        "Open in Lidarr",
      ));
    });
  }

  function createMeta(kind: DetailKind, data: JsonObject) {
    const meta = document.createElement("div");
    meta.className = "artist-meta";
    const id = document.createElement("strong");
    id.textContent = `MusicBrainz ID: ${data.id}`;
    meta.append(id);
    const plexRelease = kind === "release-group"
      ? (data.plexReleases || []).find((release: JsonObject) => release.url)
      : undefined;
    const plexUrl = data.availableInPlex
      ? (kind === "artist" ? data.plexUrl : plexRelease?.url || "")
      : "";
    const plexampUrl = data.availableInPlex
      ? (kind === "artist" ? data.plexampUrl : plexRelease?.plexampUrl || "")
      : "";
    addExternalLinks(meta, kind, data.id, data.spotify, plexUrl, plexampUrl);
    return meta;
  }

  function fillRequestSelect(select: HTMLSelectElement, options: JsonObject[], labelKey: string, valueKey: string) {
    select.replaceChildren();
    options.forEach((option) => select.add(new Option(option[labelKey], option[valueKey])));
  }

  async function openRequestDialog(artist: JsonObject, messageElement: Element = $("#detail-message")) {
    requestedArtist = artist;
    $("#dialog-artist").textContent = artist.name;
    $("#request-message").textContent = "";

    try {
      const options = await getJson("/api/lidarr/options");
      fillRequestSelect($("#request-root-folder"), options.rootFolders, "path", "path");
      fillRequestSelect($("#request-tags"), options.tags, "label", "id");
      $("#request-dialog").showModal();
    } catch (error) {
      messageElement.textContent = error.message;
    }
  }

  async function requestReleaseGroup(releaseGroup: { id: string; button: HTMLButtonElement }) {
    const button = releaseGroup.button;
    button.disabled = true;
    button.textContent = "Sending to Lidarr…";
    try {
      const result = await postJson("/api/request/release-group", { mbid: releaseGroup.id });
      showToast(result.message);
      button.textContent = result.alreadyExists
        ? "Available"
        : (result.pending ? "Queued" : "Requested");
    } catch (error) {
      showToast(error.message, true);
      button.textContent = "Request release group";
      button.disabled = false;
    }
  }

  function createSearchArtistCard(artist: JsonObject, description: string) {
    const card = createCard(artistDisplayName(artist), description, () => showDetail("artist", artist.id), artist.coverArt, "artist", artist.id);
    const requestButton = document.createElement("button");
    requestButton.className = "request";
    requestButton.type = "button";
    requestButton.textContent = "Request";
    requestButton.addEventListener("click", (event) => {
      event.stopPropagation();
      openRequestDialog(artist, $("#search-message"));
    });
    card.append(requestButton);
    return card;
  }

  function createPlexArtistCard(artist: JsonObject, description: string, plexArtist: JsonObject) {
    const card = createCard(artistDisplayName(artist), description, () => showDetail("artist", artist.id), artist.coverArt, "artist", artist.id);
    const services = document.createElement("div");
    services.className = "card-service-icons";
    const destination = mobilePlexDestination(plexArtist.url, plexampArtistUrl(plexArtist));
    services.append(createServiceIconLink(
      destination.url,
      "/icons/plex.svg",
      destination.label,
      "service-icon-link",
      destination.openInNewTab,
    ));
    card.append(services);
    getLidarrExternalUrl().then((externalUrl) => {
      if (!externalUrl) return;
      services.append(createServiceIconLink(
        `${externalUrl}/artist/${encodeURIComponent(artist.id)}`,
        "/icons/lidarr.svg",
        "Open in Lidarr",
        "service-icon-link",
      ));
    });
    return card;
  }

  function createRecommendationCarouselCard(item: JsonObject, kind: "artist" | "release-group") {
    const card = document.createElement("article");
    card.className = "recommendation-card";
    card.tabIndex = 0;
    card.setAttribute("role", "link");
    const fallback = document.createElement("div");
    fallback.className = "recommendation-art recommendation-fallback";
    let artwork: HTMLElement = fallback;
    if (item.coverArt) {
      const image = document.createElement("img");
      image.className = "recommendation-art";
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.fetchPriority = "low";
      image.width = 154;
      image.height = 154;
      loadArtworkWhenNear(image, item.coverArt, fallback);
      artwork = image;
    }
    const sourceName = item.recommendationSource || "Recommendation";
    const source = document.createElement("span");
    source.className = "recommendation-source";
    source.title = sourceName;
    source.setAttribute("aria-label", sourceName);
    const sourceIcons = [];
    if (/listenbrainz/i.test(sourceName)) {
      sourceIcons.push(["/icons/listenbrainz.svg", ""]);
    }
    if (/last\.fm/i.test(sourceName)) {
      sourceIcons.push(["/icons/last-fm.svg", ""]);
    }
    sourceIcons.forEach(([iconPath, alt]) => {
      const icon = document.createElement("img");
      icon.src = iconPath;
      icon.alt = alt;
      source.append(icon);
    });
    const sourceLabel = document.createElement("span");
    sourceLabel.className = "recommendation-source-label";
    sourceLabel.textContent = sourceName;
    source.append(sourceLabel);
    const info = document.createElement("div");
    info.className = "recommendation-info";
    const title = document.createElement("strong");
    title.textContent = item.name;
    const subtitle = document.createElement("span");
    subtitle.textContent = kind === "artist" ? (item.type || "Artist") : [item.artist, item.type].filter(Boolean).join(" · ");
    info.append(title, subtitle);
    const open = () => showDetail(kind === "artist" ? "artist" : "release-group", item.id);
    addDetailPrefetch(card, kind === "artist" ? "artist" : "release-group", item.id);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    card.append(artwork, source, info);
    const requestButton = document.createElement("button");
    requestButton.className = "recommendation-request";
    requestButton.type = "button";
    requestButton.textContent = "Request";
    requestButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (kind === "artist") openRequestDialog(item, $("#recommendations-message"));
      else requestReleaseGroup({ id: item.id, button: requestButton });
    });
    card.append(requestButton);
    return card;
  }

  function recommendationRow(title: string, items: JsonObject[], kind: "artist" | "release-group") {
    const group = document.createElement("section");
    group.className = "recommendation-row";
    const heading = document.createElement("h3"); heading.textContent = title;
    const carousel = document.createElement("div"); carousel.className = "recommendation-carousel";
    items.forEach((item) => carousel.append(createRecommendationCarouselCard(item, kind)));
    group.append(heading, carousel);
    return group;
  }

  function createReleaseGroupCard(group: JsonObject) {
    const card = createCard(
      releaseGroupDisplayTitle(group),
      [group.date, ...(group.secondaryTypes || []), group.disambiguation]
        .filter(Boolean)
        .join(" · "),
      () => showDetail("release-group", group.id),
      group.coverArt,
      "release-group",
      group.id,
    );
    const groupRequest = document.createElement("button");
    groupRequest.className = "request release-group-request";
    groupRequest.type = "button";
    if (group.fullyAvailableInLidarr) {
      groupRequest.textContent = "Available";
      groupRequest.disabled = true;
      groupRequest.title = "This release group is fully available in Lidarr";
    } else {
      groupRequest.textContent = group.availableInLidarr ? "Search missing" : "Request";
      groupRequest.addEventListener("click", (event) => {
        event.stopPropagation();
        requestReleaseGroup({ id: group.id, button: groupRequest });
      });
    }
    card.append(groupRequest);
    return card;
  }

  /**
   * Group a discography by primary release type, with secondary types opt-in.
   *
   * MusicBrainz reports a separate section per combination of primary and
   * secondary types, which for a well-documented artist means dozens of
   * headings — 28 for Radiohead, where 308 live recordings bury 10 albums.
   * Melodarr instead keeps four primary sections and exposes the secondary
   * types as filters that start switched off.
   */
  function renderDiscography(data: JsonObject) {
    const primaryOrder = ["Album", "EP", "Single", "Other"];
    const primaryLabels: Record<string, string> = {
      Album: "Albums", EP: "EPs", Single: "Singles", Other: "Other releases",
    };
    const byPrimary = new Map<string, JsonObject[]>(primaryOrder.map((name) => [name, []]));
    const secondaryCounts = new Map<string, number>();
    const searchText = new Map<JsonObject, string>();
    let releaseQuery = "";
    let wasSearching = false;

    (Object.values(data.sections || {}) as JsonObject[][]).forEach((groups) => {
      groups.forEach((group) => {
        const primary = primaryOrder.includes(group.type) ? group.type : "Other";
        byPrimary.get(primary)!.push(group);
        searchText.set(group, normalizeSearch(
          `${String(group.title || "")} ${String(group.romanizedTitle || "")} ${String(group.disambiguation || "")}`,
        ));
        (group.secondaryTypes || []).forEach((secondary: string) => {
          secondaryCounts.set(secondary, (secondaryCounts.get(secondary) || 0) + 1);
        });
      });
    });
    // Newest first: an artist's recent work is what a requester looks for, and
    // undated entries stay at the end rather than leading the list.
    byPrimary.forEach((groups) => groups.sort(
      (first, second) => (second.date || "").localeCompare(first.date || ""),
    ));

    const enabledSecondary = new Set<string>();
    const isVisible = (group: JsonObject) => (
      (!releaseQuery || searchText.get(group)?.includes(releaseQuery))
      && (Boolean(releaseQuery) || (group.secondaryTypes || [])
        .every((secondary: string) => enabledSecondary.has(secondary)))
    );

    const container = document.createDocumentFragment();
    const filter = document.createElement("div");
    filter.className = "discography-filter";
    const filterLabel = document.createElement("label");
    filterLabel.htmlFor = "discography-search";
    filterLabel.textContent = "Search releases";
    const filterInput = document.createElement("input");
    filterInput.id = "discography-search";
    filterInput.type = "search";
    filterInput.placeholder = "Search this artist's releases…";
    filterInput.autocomplete = "off";
    const filterCount = document.createElement("span");
    filterCount.setAttribute("aria-live", "polite");
    filter.append(filterLabel, filterInput, filterCount);
    const filterMessage = document.createElement("p");
    filterMessage.className = "message";
    filterMessage.setAttribute("aria-live", "polite");
    container.append(filter, filterMessage);

    const layout = document.createElement("div");
    layout.className = "discography-layout";
    const index = document.createElement("nav");
    index.className = "discography-nav";
    const content = document.createElement("div");
    content.className = "discography-content";
    const sections: Array<{
      element: HTMLDetailsElement;
      summary: HTMLElement;
      link: HTMLAnchorElement;
      groups: JsonObject[];
      rendered: boolean;
      openBeforeSearch: boolean;
    }> = [];

    function renderSection(section: (typeof sections)[number]) {
      const visible = section.groups.filter(isVisible);
      section.summary.textContent = `${section.element.dataset.label} (${visible.length})`;
      section.element.hidden = visible.length === 0;
      section.link.hidden = visible.length === 0;
      if (!section.element.open) {
        // Cards are built when a section is first opened. A large discography
        // would otherwise create hundreds of rows the reader never expands.
        section.rendered = false;
        section.element.replaceChildren(section.summary);
        return;
      }
      section.element.replaceChildren(section.summary, ...visible.map(createReleaseGroupCard));
      section.rendered = true;
    }

    primaryOrder.forEach((primary, position) => {
      const groups = byPrimary.get(primary)!;
      const section = document.createElement("details");
      section.id = `release-type-${position}`;
      section.className = "discography-section";
      section.dataset.label = primaryLabels[primary];
      section.open = primary !== "Other";
      const summary = document.createElement("summary");
      const link = document.createElement("a");
      link.href = `#${section.id}`;
      link.textContent = primaryLabels[primary];
      const entry = {
        element: section,
        summary,
        link,
        groups,
        rendered: false,
        openBeforeSearch: section.open,
      };
      section.append(summary);
      section.addEventListener("toggle", () => {
        if (section.open && !entry.rendered) renderSection(entry);
      });
      sections.push(entry);
      renderSection(entry);
      content.append(section);

      link.addEventListener("click", (event) => {
        // Keep the discography navigation inside the current rendered view.
        // Native fragment navigation changes the URL and can cause the SPA
        // route handler to re-render before the section is expanded.
        event.preventDefault();
        section.open = true;
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      index.append(link);
    });

    function refreshSections() {
      let visible = 0;
      sections.forEach((section) => {
        const matching = section.groups.filter(isVisible).length;
        visible += matching;
        if (releaseQuery && matching) section.element.open = true;
        renderSection(section);
      });
      const total = [...byPrimary.values()].reduce((count, groups) => count + groups.length, 0);
      filterCount.textContent = releaseQuery
        ? `${visible} of ${total} releases`
        : `${total} releases`;
      filterMessage.textContent = releaseQuery && !visible
        ? `No releases match “${filterInput.value.trim()}”.`
        : "";
    }

    filterInput.addEventListener("input", () => {
      const nextQuery = normalizeSearch(filterInput.value);
      const isSearching = Boolean(nextQuery);
      if (!wasSearching && isSearching) {
        sections.forEach((section) => {
          section.openBeforeSearch = section.element.open;
        });
      }
      releaseQuery = nextQuery;
      if (wasSearching && !isSearching) {
        sections.forEach((section) => {
          section.element.open = section.openBeforeSearch;
        });
      }
      wasSearching = isSearching;
      refreshSections();
    });

    if (secondaryCounts.size) {
      const filters = document.createElement("div");
      filters.className = "type-filters";
      const label = document.createElement("p");
      label.className = "type-filters-label";
      label.textContent = "Also show";
      filters.append(label);
      [...secondaryCounts.entries()]
        .sort((first, second) => second[1] - first[1])
        .forEach(([secondary, count]) => {
          const chip = document.createElement("button");
          chip.className = "type-filter";
          chip.type = "button";
          chip.setAttribute("aria-pressed", "false");
          chip.textContent = secondary;
          const total = document.createElement("span");
          total.textContent = String(count);
          chip.append(total);
          chip.addEventListener("click", () => {
            const enabled = !enabledSecondary.has(secondary);
            if (enabled) enabledSecondary.add(secondary);
            else enabledSecondary.delete(secondary);
            chip.setAttribute("aria-pressed", String(enabled));
            refreshSections();
          });
          filters.append(chip);
        });
      container.append(filters);
    }

    layout.append(index, content);
    container.append(layout);
    refreshSections();
    return container;
  }

  function renderDetail(kind: DetailKind, data: JsonObject) {
    const results = $("#detail-results");
    results.replaceChildren();
    $("#detail-message").textContent = "";

    if (kind === "artist") {
      $("#detail-eyebrow").textContent = "ARTIST DISCOGRAPHY";
      $("#detail-title").textContent = artistDisplayName(data);
      if (data.coverArtLarge) {
        loadDetailCover(data.coverArtLarge, `Artist image for ${data.name}`);
      } else {
        resetDetailCover();
      }
      $("#detail-subtitle").textContent = [data.country, data.disambiguation].filter(Boolean).join(" · ");
      const meta = createMeta("artist", data);
      const facts = [data.type, data.gender, data.area, data.lifeSpan?.begin].filter(Boolean).join(" · ");
      if (facts) meta.append(document.createElement("br"), `Artist information: ${facts}`);
      results.append(meta);
      const requestButton = document.createElement("button");
      requestButton.className = "request-artist";
      if (data.availableInLidarr) {
        requestButton.textContent = "In Lidarr";
        requestButton.disabled = true;
        requestButton.title = "This artist is already in Lidarr";
      } else {
        requestButton.textContent = "Add artist to Lidarr";
        requestButton.addEventListener("click", () => openRequestDialog(data));
      }
      const refreshButton = document.createElement("button");
      refreshButton.className = "secondary-action refresh-discography";
      refreshButton.type = "button";
      refreshButton.textContent = "Refresh discography";
      refreshButton.addEventListener("click", async () => {
        refreshButton.disabled = true;
        refreshButton.textContent = "Refreshing…";
        $("#detail-message").textContent = "Refreshing the complete discography from MusicBrainz…";
        try {
          const refreshed = await postJson(
            `/api/music/artist/${encodeURIComponent(data.id)}/refresh`,
            {},
          );
          storeSettledDetail(`artist:${data.id}`, refreshed);
          if (currentDetail?.kind === "artist" && currentDetail?.id === data.id) {
            renderDetail("artist", refreshed);
            $("#detail-message").textContent = "Discography refreshed from MusicBrainz.";
          }
        } catch (error) {
          $("#detail-message").textContent = error.message;
          refreshButton.disabled = false;
          refreshButton.textContent = "Refresh discography";
        }
      });
      const actions = document.createElement("div");
      actions.className = "detail-actions";
      actions.append(requestButton, refreshButton);
      results.append(actions);

      results.append(renderDiscography(data));
      startArtistRevalidation(data);
      return;
    }

    if (kind === "release-group") {
      $("#detail-eyebrow").textContent = "ALBUM RELEASES";
      $("#detail-title").textContent = releaseGroupDisplayTitle(data);
      if (data.coverArtLarge) {
        loadDetailCover(data.coverArtLarge, `Cover art for ${data.title}`);
      } else {
        resetDetailCover();
      }
      const subtitle = $("#detail-subtitle");
      subtitle.replaceChildren();
      if (data.artistId) {
        const artistLink = document.createElement("a");
        artistLink.className = "artist-detail-link";
        artistLink.href = detailPath("artist", data.artistId);
        artistLink.textContent = data.artist;
        artistLink.addEventListener("click", (event) => {
          event.preventDefault();
          showDetail("artist", data.artistId);
        });
        subtitle.append(artistLink);
      } else {
        subtitle.append(data.artist || "");
      }
      [data.type, data.date].filter(Boolean).forEach((value) => subtitle.append(` · ${value}`));
      results.append(createMeta("release-group", data));
      const requestButton = document.createElement("button");
      requestButton.className = "request-artist";
      if (data.fullyAvailableInLidarr) {
        requestButton.textContent = "Available";
        requestButton.disabled = true;
        requestButton.title = "This release group is fully available in Lidarr";
      } else {
        requestButton.textContent = data.availableInLidarr
          ? "Search missing"
          : "Request release group";
        requestButton.addEventListener("click", () => requestReleaseGroup({ id: data.id, button: requestButton }));
      }
      results.append(requestButton);
      data.releases.forEach((release: JsonObject) => {
        const card = createCard(
          releaseGroupDisplayTitle(release),
          [release.date, release.country, release.format, release.trackCount ? `${release.trackCount} tracks` : "", release.status, release.disambiguation].filter(Boolean).join(" · "),
          () => showDetail("release", release.id),
        );
        if (release.availableInPlex) addPlexAvailability(card, "This edition is in Plex");
        results.append(card);
      });
      return;
    }

    $("#detail-eyebrow").textContent = "RELEASE TRACKLIST";
    $("#detail-title").textContent = data.title;
    $("#detail-subtitle").textContent = [data.artist, data.date, data.country].filter(Boolean).join(" · ");
    data.tracks.forEach((track: JsonObject) => results.append(createCard(`${track.number}. ${track.title}`, track.artist || "")));
  }

  const searchTypeCopy = {
    artist: { placeholder: "Search artists…", noun: "artist" },
    album: { placeholder: "Search albums…", noun: "album" },
    track: { placeholder: "Search tracks…", noun: "release group" },
  } as const;

  function copyForSearchType(type: string) {
    return searchTypeCopy[type as keyof typeof searchTypeCopy] || searchTypeCopy.artist;
  }

  function searchResultMessage(type: string, count: number) {
    const noun = copyForSearchType(type).noun;
    const summary = `${count} ${noun}${count === 1 ? "" : "s"} found`;
    return type === "track" ? `${summary} for matching tracks` : summary;
  }

  $("#search-type").addEventListener("change", (event) => {
    const type = (event.target as HTMLSelectElement).value;
    $("#search-input").placeholder = copyForSearchType(type).placeholder;
    if ($("#search-input").value.trim().length >= 2) runSearch();
  });

  async function runSearch() {
    const requestVersion = ++searchRequestVersion;
    searchAbort?.abort();
    const query = $("#search-input").value.trim();
    const type = $("#search-type").value;
    const results = $("#results");

    if (query.length < 2) {
      results.replaceChildren();
      $("#search-message").textContent = query ? "Enter at least two characters." : "";
      return;
    }

    const controller = new AbortController();
    searchAbort = controller;
    $("#search-message").textContent = "Searching MusicBrainz…";
    $("#search-form").classList.add("searching");
    results.replaceChildren(skeletonBlock("skeleton-card", 5));
    try {
      const data = await getJson(
        `/api/search?q=${encodeURIComponent(query)}&type=${type}`,
        30_000,
        controller.signal,
      );
      if (requestVersion !== searchRequestVersion) return;
      results.replaceChildren();
      $("#search-message").textContent = data.results.length
        ? searchResultMessage(type, data.results.length)
        : "No results found.";
      data.results.forEach((result: JsonObject) => {
        const description = type === "artist"
          ? [result.type, result.country, result.disambiguation].filter(Boolean).join(" · ")
          : [
            result.artist,
            result.type,
            ...(result.secondaryTypes || []),
            result.date,
            result.disambiguation,
            type === "track" && result.matchedTrack
              ? `Matched track: ${result.matchedTrack}`
              : "",
          ].filter(Boolean).join(" · ");
        results.append(type === "artist"
          ? (result.plex ? createPlexArtistCard(result, description, result.plex) : createSearchArtistCard(result, description))
          : createCard(releaseGroupDisplayTitle(result), description, () => showDetail("release-group", result.id)));
      });
    } catch (error) {
      if (requestVersion !== searchRequestVersion) return;
      results.replaceChildren();
      $("#search-message").textContent = error.name === "AbortError"
        ? "MusicBrainz took too long to respond."
        : error.message;
    } finally {
      if (requestVersion === searchRequestVersion) {
        $("#search-form").classList.remove("searching");
      }
    }
  }

  // MusicBrainz requests are serialized at roughly one per second upstream, so
  // this waits for a genuine pause in typing rather than firing per keystroke.
  const searchDebounceMilliseconds = 450;
  $("#search-input").addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(runSearch, searchDebounceMilliseconds);
  });

  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(searchDebounce);
    runSearch();
  });

  function combineRecommendations(results: Array<{ source: string; data: JsonObject }>, key: string) {
    const combined = new Map<string, JsonObject>();
    results.forEach(({ source, data }) => data[key].forEach((item: JsonObject) => {
      const existing = combined.get(item.id);
      if (existing) {
        existing.recommendationSource = `${existing.recommendationSource} and ${source}`;
      } else {
        combined.set(item.id, { ...item, recommendationSource: source });
      }
    }));
    return [...combined.values()];
  }

  async function loadRecommendations(button: HTMLButtonElement) {
    clearTimeout(recommendationPoll);
    const message = $("#recommendations-message");
    const results = $("#recommendation-results");
    button.disabled = true;
    const placeholder = document.createElement("div");
    placeholder.className = "recommendation-carousel";
    placeholder.append(skeletonBlock("skeleton-art", 8));
    results.replaceChildren(placeholder);
    message.textContent = "Loading cached recommendations…";
    try {
      const data = await getJson("/api/discover");
      results.replaceChildren();
      if (data.pending) {
        message.textContent = "Your recommendation cache is being prepared. This page will populate automatically after the background scan finishes.";
        recommendationPoll = setTimeout(() => loadRecommendations(button), 15_000);
        return;
      }
      const artists = data.artists || [];
      const albums = data.albums || [];
      const singles = albums.filter((album: JsonObject) => /single/i.test(album.type || ""));
      const otherReleases = albums.filter((album: JsonObject) => !/single/i.test(album.type || ""));
      const providerStatus = data.providerStatus || {};
      const unavailableProviders = [];
      if (["partial", "unavailable"].includes(providerStatus.listenbrainz)) unavailableProviders.push("ListenBrainz");
      if (["partial", "unavailable"].includes(providerStatus.lastfm)) unavailableProviders.push("Last.fm");
      const retryNotice = unavailableProviders.length
        ? ` ${unavailableProviders.join(" and ")} was temporarily unavailable; available results are shown and a retry is scheduled.`
        : "";
      message.textContent = `Last refreshed ${new Date(data.refreshedAt * 1000).toLocaleString()}.${retryNotice}`;
      if (artists.length) results.append(recommendationRow("Artists", artists, "artist"));
      if (otherReleases.length) results.append(recommendationRow("Albums", otherReleases, "release-group"));
      if (singles.length) results.append(recommendationRow("Singles", singles, "release-group"));
      if (data.chartArtists?.length) results.append(recommendationRow("Popular on Last.fm", data.chartArtists, "artist"));
      (data.tagRows || []).forEach((row: JsonObject) => results.append(recommendationRow(`More for your ${row.tag} taste`, row.albums, "release-group")));
      if (!artists.length && !albums.length && !data.chartArtists?.length && !(data.tagRows || []).length && !unavailableProviders.length) message.textContent = "No MusicBrainz-linked recommendations were found in the latest scan.";
    } catch (error) {
      results.replaceChildren();
      message.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  $("#load-recommendations").addEventListener("click", () => {
    loadRecommendations($("#load-recommendations"));
  });
  window.addEventListener("melodarr-authenticated", () => loadRecommendations($("#load-recommendations")));
  window.addEventListener("melodarr-recommendations-changed", () => loadRecommendations($("#load-recommendations")));

  $("#back-to-search").addEventListener("click", () => {
    const previous = detailHistory.pop();
    if (previous) {
      // This is an in-app back action, not a new navigation. Replacing the
      // current detail URL prevents old album pages from being re-added to
      // the trail and replayed by repeated clicks.
      showDetail(previous.kind, previous.id, false, false);
      window.history.replaceState(
        detailNavigationState(previous.kind, previous.id),
        "",
        detailPath(previous.kind, previous.id),
      );
    } else {
      currentDetail = null;
      const origin = detailOrigin;
      showView(origin.view);
      window.history.replaceState(
        { view: origin.view },
        "",
        origin.view === "library" ? "/library" : "/",
      );
      window.requestAnimationFrame(() => {
        window.scrollTo({ top: origin.scrollY, left: 0, behavior: "auto" });
      });
    }
  });

  window.addEventListener("melodarr-home", () => {
    currentDetail = null;
    detailHistory.length = 0;
    searchRequestVersion += 1;
    clearTimeout(searchDebounce);
    searchAbort?.abort();
    requestedArtist = undefined;
    $("#search-form").classList.remove("searching");
    $("#search-form").reset();
    $("#search-input").placeholder = "Search artists…";
    $("#search-message").textContent = "";
    $("#results").replaceChildren();
    // Recommendation cards remain current through their own refresh events.
    // Keeping them mounted avoids refetching every thumbnail on navigation.
  });

  function showDetailFromLocation() {
    const match = window.location.pathname.match(/^\/(artists|albums|releases)\/([^/]+)$/);
    if (!match) return false;
    const routes: Record<string, DetailKind> = { artists: "artist", albums: "release-group", releases: "release" };
    const kind = routes[match[1]];
    const stateOrigin = window.history.state?.detailOrigin as DetailOrigin | undefined;
    detailOrigin = stateOrigin?.view === "library"
      ? { view: "library", scrollY: Number(stateOrigin.scrollY) || 0 }
      : { view: "discover", scrollY: Number(stateOrigin?.scrollY) || 0 };
    const stateHistory = window.history.state?.detailHistory;
    detailHistory.length = 0;
    if (Array.isArray(stateHistory)) {
      stateHistory.forEach((entry: DetailReference) => {
        if (
          ["artist", "release-group", "release"].includes(entry?.kind)
          && typeof entry.id === "string"
        ) {
          detailHistory.push(entry);
        }
      });
    }
    showDetail(kind, decodeURIComponent(match[2]), false, false);
    return true;
  }

  window.addEventListener("popstate", () => {
    if (showDetailFromLocation()) return;
    currentDetail = null;
    detailHistory.length = 0;
  });
  window.addEventListener("melodarr-open-detail", (event) => {
    const detail = (event as CustomEvent<{ kind: DetailKind; id: string }>).detail;
    if (detail?.kind && detail?.id) showDetail(detail.kind, detail.id);
  });
  showDetailFromLocation();

  const backToTop = $("#back-to-top");
  backToTop.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
  window.addEventListener("scroll", () => {
    backToTop.classList.toggle("visible", window.scrollY > 400);
  }, { passive: true });

  document.querySelectorAll(".close-dialog").forEach((button) => {
    button.addEventListener("click", () => $("#request-dialog").close());
  });

  $("#request-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const advanced = $("#request-dialog details");
    if (!requestedArtist) return;
    const body: JsonObject = { mbid: requestedArtist.id };
    if (advanced.open) {
      body.rootFolderPath = $("#request-root-folder").value;
      body.tags = [...$("#request-tags").selectedOptions].map((option) => Number(option.value));
      body.searchForMissingAlbums = $("#request-search").checked;
    }

    try {
      const result = await postJson("/api/request", body);
      $("#request-dialog").close();
      showToast(result.message);
    } catch (error) {
      $("#request-message").textContent = error.message;
    }
  });
})();
