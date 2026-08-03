(() => {
  type DetailKind = "artist" | "release-group" | "release" | "anime" | "series";
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
  type DetailAvailabilityWatcher = {
    kind: "artist" | "release-group";
    id: string;
    data: JsonObject;
    timer?: ReturnType<typeof setTimeout>;
  };
  type AnimeResolutionWatcher = {
    slug: string;
    data: JsonObject;
    attempts: number;
    maxAttempts: number;
    requestedThemeIds: Set<string>;
    observer?: IntersectionObserver;
    timer?: ReturnType<typeof setTimeout>;
  };
  type AnimeDetailUiState = {
    initialized: boolean;
    manageMappings: boolean;
    openSections: Set<string>;
  };

  const $ = <T extends Element = AppElement>(selector: string): T => {
    const element = document.querySelector<T>(selector);
    if (!element) throw new Error(`Required element not found: ${selector}`);
    return element;
  };
  let currentDetail: DetailReference | null = null;
  let currentDetailData: JsonObject | undefined;
  const detailHistory: DetailReference[] = [];
  let detailOrigin: DetailOrigin = { view: "discover", scrollY: 0 };
  let requestedArtist: JsonObject | undefined;
  let lidarrExternalUrl: string | undefined;
  let lidarrExternalUrlRequest: Promise<string> | undefined;
  let lidarrExternalUrlVersion = 0;
  let recommendationPoll: ReturnType<typeof setTimeout> | undefined;
  let recommendationRequestVersion = 0;
  let recommendationAbort: AbortController | undefined;
  let searchRequestVersion = 0;
  let searchDebounce: ReturnType<typeof setTimeout>;
  let searchAbort: AbortController | undefined;
  const detailRequests = new Map<string, DetailRequest>();
  const detailUpgrades = new Map<string, Promise<JsonObject>>();
  let artistRevalidation: ArtistRevalidation | undefined;
  let detailAvailabilityWatcher: DetailAvailabilityWatcher | undefined;
  let animeResolutionWatcher: AnimeResolutionWatcher | undefined;
  const animeDetailUi = new Map<string, AnimeDetailUiState>();
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
    const abort = () => controller.abort();
    // A superseded typeahead query aborts through the caller's signal, while
    // the timeout above still bounds every request.
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });

    try {
      const response = await fetch(url, { signal: controller.signal });
      handleAuthenticationFailure(response);
      if (response.status === 204) {
        const error = new Error("Requested detail is not cached.");
        error.name = "CacheMissError";
        throw error;
      }
      const body = await response.json() as JsonObject;
      if (!response.ok) throw new Error(body.error || "MusicBrainz couldn’t complete that request just now.");
      return body;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }
  }

  async function getLidarrExternalUrl() {
    if (currentUser?.role !== "admin") return "";
    if (lidarrExternalUrl !== undefined) return lidarrExternalUrl;
    if (lidarrExternalUrlRequest) return lidarrExternalUrlRequest;

    const requestVersion = lidarrExternalUrlVersion;
    const request = getJson("/api/settings")
      .then((settings) => {
        const externalUrl = String(settings.lidarr.externalUrl || "");
        if (requestVersion !== lidarrExternalUrlVersion) return "";
        lidarrExternalUrl = externalUrl;
        return externalUrl;
      })
      .catch(() => "");
    lidarrExternalUrlRequest = request;
    request.finally(() => {
      if (lidarrExternalUrlRequest === request) {
        lidarrExternalUrlRequest = undefined;
      }
    });
    return request;
  }

  function postJson(url: string, body: JsonObject): Promise<JsonObject> {
    return api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function showView(id: AppView) {
    if (id !== "detail") {
      stopArtistRevalidation();
      stopDetailAvailability();
      stopAnimeResolution();
      currentDetailData = undefined;
    }
    document.querySelectorAll(".view, .nav-link").forEach((element) => element.classList.remove("active"));
    $(`#${id}`).classList.add("active");
    const currentNavigationView = id === "detail" ? detailOrigin.view : id;
    document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => {
      const isCurrent = button.dataset.view === currentNavigationView;
      button.classList.toggle("active", isCurrent);
      if (isCurrent) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
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
    if (onClick) {
      const openButton = document.createElement("button");
      openButton.className = "card-open";
      openButton.type = "button";
      openButton.setAttribute("aria-label", `Open details for ${title}`);
      openButton.append(artwork, info);
      openButton.addEventListener("click", onClick);
      if (detailKind && detailId) addDetailPrefetch(openButton, detailKind, detailId);
      card.append(openButton);
    } else {
      card.append(artwork, info);
    }
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
    if (element.querySelector(".plex-availability")) return;
    const badge = document.createElement("span");
    badge.className = "plex-availability";
    badge.textContent = label;
    element.append(badge);
  }

  function detailPath(kind: DetailKind, id: string) {
    const route: Record<DetailKind, string> = {
      artist: "artists",
      "release-group": "albums",
      release: "releases",
      anime: "anime",
      series: "series",
    };
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
    const endpoint = kind === "anime" || kind === "series"
      ? `/api/${kind}/${encodeURIComponent(id)}`
      : `/api/music/${kind}/${encodeURIComponent(id)}`;
    entry.promise = getJson(
      `${endpoint}${query}`,
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
    stopDetailAvailability();
    stopAnimeResolution();
    currentDetailData = undefined;
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
    const previousLabel = previous?.kind === "artist"
      ? "artist"
      : previous?.kind === "release-group"
        ? "album"
        : previous?.kind === "anime"
          ? "anime"
          : previous?.kind === "series" ? "series" : "release";
    $("#back-to-search").textContent = previous
      ? `← Back to ${previousLabel}`
      : detailOrigin.view === "library" ? "← Back to library" : "← Back to search";
    showView("detail");
    const detailResults = $("#detail-results");
    detailResults.setAttribute("aria-busy", "true");
    detailResults.replaceChildren(skeletonBlock("skeleton-card", kind === "release" ? 6 : 4));
    $("#detail-title").textContent = "";
    $("#detail-eyebrow").textContent = "";
    $("#detail-subtitle").textContent = "";
    $("#detail-cover").classList.toggle("anime-cover", kind === "anime");
    resetDetailCover(kind !== "release" && kind !== "series");
    $("#detail-message").textContent = kind === "artist"
      ? "Loading artist and discography…"
      : kind === "release-group"
        ? "Loading album and release information…"
        : kind === "anime"
          ? "Loading anime themes…"
          : kind === "series" ? "Loading related anime…" : "Loading release…";

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
        const provider = kind === "anime" || kind === "series" ? "AnimeThemes" : "MusicBrainz";
        $("#detail-message").textContent = error.name === "AbortError"
          ? `${provider} is taking a little longer than usual. Please try again in a moment.`
          : `We couldn’t load this page just now. ${error.message}`;
        const retry = document.createElement("button");
        retry.className = "outline";
        retry.type = "button";
        retry.textContent = kind === "artist" ? "Retry discography" : "Retry";
        retry.addEventListener("click", () => showDetail(kind, id, false, false));
        detailResults.replaceChildren(retry);
      })
      .finally(() => {
        if (currentDetail?.kind === kind && currentDetail?.id === id) {
          detailResults.removeAttribute("aria-busy");
        }
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
    image.width = 24;
    image.height = 24;
    image.decoding = "async";
    link.append(image);
    return link;
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
      createServiceIconLink(url, icon, label, "external-link-musicbrainz"),
    ));
    if (spotify) {
      const mobile = isMobileDevice();
      links.append(createServiceIconLink(
        spotify,
        "/icons/spotify.svg",
        mobile ? "Open in Spotify" : "Open on Spotify",
        "external-link-spotify",
        !mobile,
      ));
    }
    if (plexUrl) {
      const destination = mobilePlexDestination(plexUrl, plexampUrl);
      links.append(createServiceIconLink(
        destination.url,
        "/icons/plex.svg",
        destination.label,
        "external-link-plex",
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
        "external-link-lidarr",
      ));
    });
  }

  function detailPlexLinks(kind: "artist" | "release-group", data: JsonObject) {
    const plexRelease = kind === "release-group"
      ? (data.plexReleases || []).find((release: JsonObject) => release.url)
      : undefined;
    return {
      url: data.availableInPlex
        ? String(kind === "artist" ? data.plexUrl || "" : plexRelease?.url || "")
        : "",
      plexampUrl: data.availableInPlex
        ? String(
          kind === "artist"
            ? data.plexampUrl || ""
            : plexRelease?.plexampUrl || "",
        )
        : "",
    };
  }

  function updateDetailPlexLink(kind: "artist" | "release-group", data: JsonObject) {
    const links = $("#detail-results").querySelector(".artist-meta .external-icons");
    if (!links) return;
    const existing = links.querySelector(".external-link-plex");
    const destinationUrls = detailPlexLinks(kind, data);
    if (!destinationUrls.url) {
      existing?.remove();
      return;
    }
    const destination = mobilePlexDestination(
      destinationUrls.url,
      destinationUrls.plexampUrl,
    );
    const updated = createServiceIconLink(
      destination.url,
      "/icons/plex.svg",
      destination.label,
      "external-link-plex",
      destination.openInNewTab,
    );
    if (existing) {
      existing.replaceWith(updated);
      return;
    }
    const lidarrLink = links.querySelector(".external-link-lidarr");
    links.insertBefore(updated, lidarrLink);
  }

  function createMeta(kind: DetailKind, data: JsonObject) {
    const meta = document.createElement("div");
    meta.className = "artist-meta";
    const id = document.createElement("strong");
    id.textContent = `MusicBrainz ID: ${data.id}`;
    meta.append(id);
    const plexLinks = kind === "artist" || kind === "release-group"
      ? detailPlexLinks(kind, data)
      : { url: "", plexampUrl: "" };
    addExternalLinks(
      meta,
      kind,
      data.id,
      data.spotify,
      plexLinks.url,
      plexLinks.plexampUrl,
    );
    return meta;
  }

  function stopDetailAvailability() {
    if (detailAvailabilityWatcher?.timer) {
      clearTimeout(detailAvailabilityWatcher.timer);
    }
    detailAvailabilityWatcher = undefined;
  }

  function artistReleaseGroups(data: JsonObject) {
    return (Object.values(data.sections || {}) as JsonObject[][]).flat();
  }

  function incompleteArtistReleaseGroups(data: JsonObject) {
    return artistReleaseGroups(data).filter(
      (group) => group.availableInLidarr && !group.fullyAvailableInLidarr,
    );
  }

  function applyArtistReleaseGroupAvailability(
    data: JsonObject,
    updates: JsonObject,
  ) {
    const groups = new Map(
      artistReleaseGroups(data).map((group) => [String(group.id), group]),
    );
    Object.entries(updates || {}).forEach(([id, status]: [string, JsonObject]) => {
      const group = groups.get(id);
      if (!group) return;
      group.availableInPlex = Boolean(
        group.availableInPlex || status.availableInPlex,
      );
      group.availableInLidarr = Boolean(
        group.availableInLidarr || status.availableInLidarr,
      );
      group.fullyAvailableInLidarr = Boolean(
        group.fullyAvailableInLidarr || status.fullyAvailableInLidarr,
      );
      if (status.availableInLidarr) group.availabilityPending = false;
    });

    $("#detail-results").querySelectorAll<HTMLElement>("[data-release-group-id]")
      .forEach((card) => {
        const group = groups.get(String(card.dataset.releaseGroupId));
        const button = card.querySelector<HTMLButtonElement>(
          ".release-group-request",
        );
        if (!group || !button) return;
        if (group.fullyAvailableInLidarr) {
          button.textContent = "Available";
          button.disabled = true;
          button.title = "This release group is fully available in Lidarr";
        } else if (group.availableInLidarr && !group.availabilityPending) {
          button.textContent = "Search missing";
          button.disabled = false;
          button.title = "";
        }
      });
  }

  function applyDetailAvailability(
    watcher: DetailAvailabilityWatcher,
    availability: JsonObject,
  ) {
    Object.assign(watcher.data, availability);
    updateDetailPlexLink(watcher.kind, watcher.data);
    const action = $("#detail-results")
      .querySelector<HTMLButtonElement>(".detail-availability-action");

    if (watcher.kind === "artist") {
      if (action && watcher.data.availableInLidarr) {
        action.textContent = "In Lidarr";
        action.disabled = true;
        action.title = "This artist is already in Lidarr";
      }
      applyArtistReleaseGroupAvailability(
        watcher.data,
        availability.releaseGroups || {},
      );
      return;
    }

    if (action) {
      if (watcher.data.fullyAvailableInLidarr) {
        action.textContent = "Available";
        action.disabled = true;
        action.title = "This release group is fully available in Lidarr";
      } else if (watcher.data.availableInLidarr) {
        action.textContent = "Search missing";
      }
    }

    const ownedReleaseIds = new Set(
      (watcher.data.ownedReleaseIds || []).map(String),
    );
    (watcher.data.releases || []).forEach((release: JsonObject) => {
      release.availableInPlex = ownedReleaseIds.has(String(release.id));
    });
    $("#detail-results").querySelectorAll<HTMLElement>("[data-release-id]")
      .forEach((card) => {
        const available = ownedReleaseIds.has(String(card.dataset.releaseId));
        const badge = card.querySelector(".plex-availability");
        if (available) addPlexAvailability(card, "This edition is in Plex");
        else badge?.remove();
      });
  }

  function scheduleDetailAvailability(
    watcher: DetailAvailabilityWatcher,
    delay = 15_000,
  ) {
    if (
      detailAvailabilityWatcher !== watcher
      || document.visibilityState === "hidden"
    ) return;
    watcher.timer = setTimeout(() => pollDetailAvailability(watcher), delay);
  }

  async function pollDetailAvailability(watcher: DetailAvailabilityWatcher) {
    watcher.timer = undefined;
    if (
      detailAvailabilityWatcher !== watcher
      || currentDetail?.kind !== watcher.kind
      || currentDetail.id !== watcher.id
      || !$("#detail").classList.contains("active")
    ) return;
    if (document.visibilityState === "hidden") return;

    try {
      const availabilityUrl = new URL(
        `/api/music/${watcher.kind}/${encodeURIComponent(watcher.id)}/availability`,
        window.location.origin,
      );
      if (watcher.kind === "artist") {
        incompleteArtistReleaseGroups(watcher.data)
          .slice(0, 50)
          .forEach((group) => {
            availabilityUrl.searchParams.append(
              "releaseGroup",
              String(group.id),
            );
          });
      }
      const availability = await getJson(
        `${availabilityUrl.pathname}${availabilityUrl.search}`,
      );
      if (
        detailAvailabilityWatcher !== watcher
        || currentDetail?.kind !== watcher.kind
        || currentDetail.id !== watcher.id
      ) return;
      applyDetailAvailability(watcher, availability);
      if (
        availability.settled
        && (
          watcher.kind !== "artist"
          || !incompleteArtistReleaseGroups(watcher.data).length
        )
      ) {
        stopDetailAvailability();
      } else {
        scheduleDetailAvailability(watcher);
      }
    } catch {
      if (detailAvailabilityWatcher === watcher) {
        scheduleDetailAvailability(watcher, 30_000);
      }
    }
  }

  function startDetailAvailability(
    kind: DetailKind,
    data: JsonObject,
    delay = 5_000,
  ) {
    stopDetailAvailability();
    if (kind !== "artist" && kind !== "release-group") return;
    const watcher: DetailAvailabilityWatcher = {
      kind,
      id: String(data.id),
      data,
    };
    detailAvailabilityWatcher = watcher;
    scheduleDetailAvailability(watcher, delay);
  }

  document.addEventListener("visibilitychange", () => {
    const watcher = detailAvailabilityWatcher;
    if (
      document.visibilityState === "visible"
      && watcher
      && !watcher.timer
    ) {
      scheduleDetailAvailability(watcher, 0);
    }
  });

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

  async function requestReleaseGroup(releaseGroup: {
    id: string;
    button: HTMLButtonElement;
    animeContext?: JsonObject;
  }) {
    const button = releaseGroup.button;
    button.disabled = true;
    button.textContent = "Sending to Lidarr…";
    try {
      const result = await postJson("/api/request/release-group", {
        mbid: releaseGroup.id,
        ...(releaseGroup.animeContext || {}),
      });
      showToast(result.message);
      button.textContent = result.alreadyExists
        ? "Available"
        : (result.pending ? "Queued" : "Requested");
      if (
        currentDetail?.kind === "release-group"
        && currentDetail.id === releaseGroup.id
        && currentDetailData
      ) {
        startDetailAvailability("release-group", currentDetailData, 0);
      } else if (
        currentDetail?.kind === "artist"
        && currentDetailData
      ) {
        const group = artistReleaseGroups(currentDetailData)
          .find((item) => String(item.id) === releaseGroup.id);
        if (group) {
          // Keep the just-requested state stable until the Lidarr library
          // snapshot observes it, then transition to Search missing/Available.
          group.availableInLidarr = true;
          group.availabilityPending = true;
          startDetailAvailability("artist", currentDetailData, 0);
        }
      }
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
    if (/plex/i.test(sourceName)) {
      sourceIcons.unshift(["/icons/plex.svg", ""]);
    }
    sourceIcons.forEach(([iconPath, alt]) => {
      const icon = document.createElement("img");
      icon.src = iconPath;
      icon.alt = alt;
      icon.width = 13;
      icon.height = 13;
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
    const openButton = document.createElement("button");
    openButton.className = "recommendation-open";
    openButton.type = "button";
    openButton.setAttribute("aria-label", `Open details for ${item.name}`);
    openButton.append(artwork, source, info);
    openButton.addEventListener("click", open);
    addDetailPrefetch(openButton, kind === "artist" ? "artist" : "release-group", item.id);
    card.append(openButton);
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
    group.append(heading, carousel);
    const batchSize = window.matchMedia("(max-width: 700px)").matches ? 6 : 8;
    let rendered = 0;
    const more = document.createElement("button");
    more.className = "outline recommendation-more";
    more.type = "button";

    const renderMore = () => {
      const end = Math.min(rendered + batchSize, items.length);
      const fragment = document.createDocumentFragment();
      for (let index = rendered; index < end; index += 1) {
        fragment.append(createRecommendationCarouselCard(items[index], kind));
      }
      carousel.append(fragment);
      rendered = end;
      if (rendered >= items.length) {
        more.remove();
      } else {
        more.textContent = `Show ${Math.min(batchSize, items.length - rendered)} more`;
        more.setAttribute("aria-label", `Show more from ${title}`);
      }
    };

    more.addEventListener("click", renderMore);
    renderMore();
    if (rendered < items.length) group.append(more);
    return group;
  }

  function deferredTasteRows(rows: JsonObject[]) {
    const group = document.createElement("section");
    group.className = "recommendation-later";
    const copy = document.createElement("p");
    copy.textContent = `${rows.length} more ${rows.length === 1 ? "shelf is" : "shelves are"} ready from your listening tastes.`;
    const reveal = document.createElement("button");
    reveal.className = "outline";
    reveal.type = "button";
    reveal.textContent = "Show more recommendations";
    reveal.addEventListener("click", () => {
      const fragment = document.createDocumentFragment();
      rows.forEach((row) => fragment.append(
        recommendationRow(`More for your ${row.tag} taste`, row.albums, "release-group"),
      ));
      group.className = "recommendation-expanded";
      group.replaceChildren(fragment);
      const firstHeading = group.querySelector<HTMLElement>("h3");
      if (firstHeading) {
        firstHeading.tabIndex = -1;
        firstHeading.focus();
      }
    }, { once: true });
    group.append(copy, reveal);
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
    card.dataset.releaseGroupId = String(group.id);
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
    let filterFrame: number | undefined;

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
    const totalReleaseCount = [...byPrimary.values()]
      .reduce((count, groups) => count + groups.length, 0);

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

    function renderSection(
      section: (typeof sections)[number],
      visible = section.groups.filter(isVisible),
    ) {
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
        const visibleGroups = section.groups.filter(isVisible);
        visible += visibleGroups.length;
        if (releaseQuery && visibleGroups.length) section.element.open = true;
        renderSection(section, visibleGroups);
      });
      filterCount.textContent = releaseQuery
        ? `${visible} of ${totalReleaseCount} releases`
        : `${totalReleaseCount} releases`;
      filterMessage.textContent = releaseQuery && !visible
        ? `No releases match “${filterInput.value.trim()}”.`
        : "";
    }

    filterInput.addEventListener("input", () => {
      if (filterFrame !== undefined) return;
      filterFrame = window.requestAnimationFrame(() => {
        filterFrame = undefined;
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

  function animeThemeKind(theme: JsonObject) {
    const type = String(theme.type || "").toLowerCase();
    if (type === "op" || type.startsWith("open")) return "opening";
    if (type === "ed" || type.startsWith("end")) return "ending";
    return "other";
  }

  function animeThemeLabel(theme: JsonObject) {
    if (theme.label) return String(theme.label);
    const kind = animeThemeKind(theme);
    const prefix = kind === "opening" ? "Opening" : kind === "ending" ? "Ending" : "Theme";
    return theme.sequence === undefined || theme.sequence === null || theme.sequence === ""
      ? prefix
      : `${prefix} ${theme.sequence}`;
  }

  function formatEpisodeRange(value: JsonObject | number | string) {
    if (typeof value === "number" || typeof value === "string") return String(value);
    const start = value.start ?? value.from ?? value.begin;
    const end = value.end ?? value.to ?? value.stop;
    if (start !== undefined && end !== undefined && String(start) !== String(end)) {
      return `${start}–${end}`;
    }
    return String(start ?? end ?? value.label ?? "");
  }

  function animeEpisodes(episodes: unknown) {
    if (!episodes) return "";
    let range = "";
    if (Array.isArray(episodes)) {
      const numeric = episodes.every((episode) => Number.isFinite(Number(episode)))
        ? episodes.map(Number).sort((first, second) => first - second)
        : [];
      if (numeric.length) {
        const ranges: string[] = [];
        let start = numeric[0];
        let end = numeric[0];
        numeric.slice(1).forEach((episode) => {
          if (episode === end + 1) {
            end = episode;
          } else {
            ranges.push(start === end ? String(start) : `${start}–${end}`);
            start = episode;
            end = episode;
          }
        });
        ranges.push(start === end ? String(start) : `${start}–${end}`);
        range = ranges.join(", ");
      } else {
        range = episodes.map(formatEpisodeRange).filter(Boolean).join(", ");
      }
    } else if (typeof episodes === "object") {
      range = formatEpisodeRange(episodes as JsonObject);
    } else {
      range = String(episodes).trim();
    }
    if (!range) return "";
    if (/^episodes?\b/i.test(range)) return range;
    return `${range.includes(",") || range.includes("–") || range.includes("-") ? "Episodes" : "Episode"} ${range}`;
  }

  function animeArtistNames(song: JsonObject) {
    return (song.artists || [])
      .map((artist: JsonObject | string) => typeof artist === "string" ? artist : artist.name)
      .filter(Boolean)
      .join(", ");
  }

  function animeMappingState(mapping: JsonObject | undefined, unavailable = false) {
    const state = String(mapping?.state || mapping?.status || "").toLowerCase();
    if (["resolved", "matched", "mapped"].includes(state)) return "resolved";
    if (["ambiguous", "candidates", "choose"].includes(state)) return "ambiguous";
    if (["unmatched", "not-found", "not_found", "none"].includes(state)) return "unmatched";
    if (["failed", "error"].includes(state)) return "failed";
    return unavailable ? "unavailable" : "pending";
  }

  function animeRequestContext(theme: JsonObject) {
    return {
      animeSlug: String(currentDetail?.id || currentDetailData?.slug || ""),
      animeName: String(currentDetailData?.name || ""),
      themeId: String(theme.id || ""),
      themeLabel: animeThemeLabel(theme),
      songId: String(theme.song?.id || ""),
      songTitle: String(theme.song?.title || ""),
    };
  }

  function animeCandidatePlexLink(candidate: JsonObject) {
    if (!candidate.availableInPlex) return null;
    const plexRelease = (candidate.plexReleases || [])
      .find((release: JsonObject) => release.url || release.plexampUrl);
    const destination = mobilePlexDestination(
      String(candidate.plexUrl || plexRelease?.url || ""),
      String(candidate.plexampUrl || plexRelease?.plexampUrl || ""),
    );
    if (!destination.url) return null;
    return createServiceIconLink(
      destination.url,
      "/icons/plex.svg",
      destination.label,
      "service-icon-link anime-candidate-plex",
      destination.openInNewTab,
    );
  }

  function createAnimeReleaseCandidate(
    candidate: JsonObject,
    theme: JsonObject,
    recommended = false,
    confirmMatch?: (button: HTMLButtonElement) => void,
  ) {
    const id = String(candidate.id || "");
    const description = [
      candidate.artist,
      candidate.type,
      ...(candidate.secondaryTypes || []),
      candidate.date,
    ].filter(Boolean).join(" · ");
    const card = createCard(
      releaseGroupDisplayTitle(candidate),
      description,
      id ? () => showDetail("release-group", id) : undefined,
      candidate.coverArt,
      id ? "release-group" : undefined,
      id,
    );
    card.classList.add("anime-release-candidate");
    if (recommended) {
      const badge = document.createElement("span");
      badge.className = "anime-recommended";
      badge.textContent = "Recommended";
      card.append(badge);
    }
    if (id) {
      const requestButton = document.createElement("button");
      requestButton.className = "request release-group-request";
      requestButton.type = "button";
      if (candidate.fullyAvailableInLidarr) {
        requestButton.textContent = "Available";
        requestButton.disabled = true;
      } else {
        requestButton.textContent = candidate.availableInLidarr ? "Search missing" : "Request";
        requestButton.addEventListener("click", (event) => {
          event.stopPropagation();
          requestReleaseGroup({
            id,
            button: requestButton,
            animeContext: animeRequestContext(theme),
          });
        });
      }
      card.append(requestButton);
    }
    if (confirmMatch && id) {
      const confirm = document.createElement("button");
      confirm.className = "anime-candidate-confirm";
      confirm.type = "button";
      confirm.textContent = "Confirm match";
      confirm.addEventListener("click", (event) => {
        event.stopPropagation();
        confirmMatch(confirm);
      });
      card.append(confirm);
    }
    const plexLink = animeCandidatePlexLink(candidate);
    if (plexLink) card.append(plexLink);
    return card;
  }

  function animeMappingProvenance(mapping: JsonObject | undefined, state: string) {
    const provenance = String(
      mapping?.registryProvenance || mapping?.provenance || "",
    ).toLowerCase();
    const method = String(mapping?.matchMethod || "").toLowerCase();
    const review = String(
      mapping?.registryStatus || mapping?.mappingStatus || "",
    ).toLowerCase();
    if (provenance === "manual-confirmation") return "Automatic match · confirmed";
    if (provenance === "manual") {
      return review === "confirmed" ? "Manual · confirmed" : "Manual mapping";
    }
    if (mapping?.mappingSource === "local") {
      return `Local registry${review ? ` · ${review}` : ""}`;
    }
    if (mapping?.mappingSource === "seed") return "Verified catalog mapping";
    if (method === "recording-search") return "Automatic · recording match";
    if (method === "artist-discography-title") return "Automatic · artist/title match";
    if (state === "resolved" || state === "ambiguous") return "Automatic match";
    return "Automatic lookup";
  }

  function updateAnimeThemeMapping(theme: JsonObject, payload: JsonObject) {
    const affectedReleaseGroupIds = [theme.mapping, payload.mapping]
      .flatMap((mapping: JsonObject | undefined) => [
        mapping?.preferredReleaseGroupId,
        ...(mapping?.releaseGroups || []).map((group: JsonObject) => group.id),
      ])
      .filter(Boolean);
    affectedReleaseGroupIds.forEach((id) => {
      detailRequests.delete(`release-group:${String(id)}`);
    });
    if (payload.theme) Object.assign(theme, payload.theme);
    if (payload.mapping !== undefined) theme.mapping = payload.mapping || {};
    if (payload.myProposal !== undefined) theme.myProposal = payload.myProposal;
    if (payload.proposals !== undefined) theme.proposals = payload.proposals;
    if (payload.proposal !== undefined) theme.myProposal = payload.proposal;
    if (
      currentDetail?.kind === "anime"
      && currentDetailData
      && (currentDetailData.themes || []).includes(theme)
    ) {
      currentDetailData.resolutionUnavailable = false;
      renderAnimeDetail(currentDetailData);
    }
    if (payload.message) showToast(String(payload.message));
  }

  function musicBrainzThemeSearchUrl(theme: JsonObject) {
    const artist = animeArtistNames(theme.song || {});
    const knownArtist = /^(unknown|unknown artist)$/i.test(artist.trim()) ? "" : artist;
    const search = [theme.song?.title, knownArtist]
      .filter(Boolean)
      .join(" ");
    const url = new URL("https://musicbrainz.org/search");
    url.searchParams.set("query", search);
    url.searchParams.set("type", "release_group");
    url.searchParams.set("method", "indexed");
    return url.href;
  }

  function createAnimeMappingField(theme: JsonObject, initialValue = "") {
    const field = document.createElement("div");
    field.className = "anime-mapping-field";
    const label = document.createElement("label");
    const inputId = `anime-mapping-${theme.id}-${Math.random().toString(36).slice(2)}`;
    label.htmlFor = inputId;
    label.textContent = "MusicBrainz release group";
    const input = document.createElement("input");
    input.id = inputId;
    input.name = "releaseGroup";
    input.required = true;
    input.autocomplete = "off";
    input.placeholder = "Release-group URL or MBID";
    input.value = initialValue;
    const search = document.createElement("a");
    search.className = "anime-musicbrainz-search";
    search.href = musicBrainzThemeSearchUrl(theme);
    search.target = "_blank";
    search.rel = "noreferrer";
    search.textContent = "Search MusicBrainz";
    field.append(label, input, search);
    return { field, input };
  }

  function proposalReleaseGroupId(proposal: JsonObject | undefined) {
    return String(
      proposal?.releaseGroupId
      || proposal?.release_group_id
      || proposal?.releaseGroupMbid
      || proposal?.release_group_mbid
      || proposal?.releaseGroup?.id
      || proposal?.mbid
      || "",
    );
  }

  function createAnimeMappingEditor(
    theme: JsonObject,
    mapping: JsonObject | undefined,
    manageMappings: boolean,
  ) {
    const isAdmin = currentUser?.role === "admin";
    if (!theme.id || (isAdmin && !manageMappings)) return null;
    const details = document.createElement("details");
    details.className = "anime-mapping-editor";
    const summary = document.createElement("summary");
    const localMapping = mapping?.mappingSource === "local";
    const seedMapping = mapping?.mappingSource === "seed";
    const myProposal = (theme.myProposal || mapping?.myProposal) as JsonObject | undefined;
    const proposalStatus = String(myProposal?.status || "pending").toLowerCase();
    summary.textContent = isAdmin
      ? (localMapping
        ? "Correct mapping"
        : mapping?.releaseGroups?.length ? "Override mapping" : "Link release group")
      : myProposal && ["pending", "submitted", "review"].includes(proposalStatus)
        ? "Mapping suggestion pending"
        : "Suggest a mapping";

    const form = document.createElement("form");
    const initialValue = isAdmin
      ? String(mapping?.preferredReleaseGroupId || mapping?.releaseGroups?.[0]?.id || "")
      : proposalReleaseGroupId(myProposal);
    const { field, input } = createAnimeMappingField(theme, initialValue);

    const message = document.createElement("p");
    message.className = "anime-mapping-editor-message";
    message.setAttribute("role", "status");
    const actions = document.createElement("div");
    actions.className = "anime-mapping-editor-actions";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = isAdmin
      ? (localMapping ? "Save correction" : "Save mapping")
      : "Submit for admin review";
    actions.append(save);

    const themeEndpoint = `/api/anime/${encodeURIComponent(String(currentDetail?.id || ""))}`
      + `/themes/${encodeURIComponent(String(theme.id))}`;
    const endpoint = isAdmin ? `${themeEndpoint}/mapping` : `${themeEndpoint}/mapping-proposals`;
    if (!isAdmin && myProposal && ["pending", "submitted", "review"].includes(proposalStatus)) {
      save.remove();
      input.disabled = true;
      message.textContent = "Pending admin review. Your suggestion is only visible to you and administrators.";
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!isAdmin && myProposal && ["pending", "submitted", "review"].includes(proposalStatus)) return;
      save.disabled = true;
      message.classList.remove("error");
      message.textContent = isAdmin ? "Verifying with MusicBrainz…" : "Submitting suggestion…";
      try {
        const payload = isAdmin
          ? await api(endpoint, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ releaseGroup: input.value.trim() }),
            })
          : await postJson(endpoint, { releaseGroup: input.value.trim() });
        updateAnimeThemeMapping(theme, payload);
      } catch (error) {
        message.classList.add("error");
        message.textContent = error.message;
        save.disabled = false;
      }
    });

    if (isAdmin && (localMapping || seedMapping)) {
      const unlink = document.createElement("button");
      unlink.type = "button";
      unlink.className = "anime-mapping-unlink";
      unlink.textContent = seedMapping ? "Suppress" : "Unlink";
      unlink.addEventListener("click", async () => {
        const prompt = seedMapping
          ? "Suppress this built-in mapping on this Melodarr instance?"
          : "Remove this local anime theme mapping?";
        if (!window.confirm(prompt)) return;
        unlink.disabled = true;
        save.disabled = true;
        message.classList.remove("error");
        message.textContent = "Removing local mapping…";
        try {
          const payload = await api(endpoint, { method: "DELETE" });
          updateAnimeThemeMapping(theme, payload);
        } catch (error) {
          message.classList.add("error");
          message.textContent = error.message;
          unlink.disabled = false;
          save.disabled = false;
        }
      });
      actions.append(unlink);
    }
    form.append(field, actions, message);
    details.append(summary, form);
    return details;
  }

  function createAnimeProposalReview(theme: JsonObject, mapping: JsonObject | undefined) {
    const proposals = (theme.proposals || mapping?.proposals || []) as JsonObject[];
    const pending = proposals.filter((proposal) => (
      !proposal.status
      || ["pending", "submitted", "review"].includes(String(proposal.status).toLowerCase())
    ));
    if (!pending.length) return null;
    const details = document.createElement("details");
    details.className = "anime-mapping-proposals";
    const summary = document.createElement("summary");
    summary.textContent = `${pending.length} user ${pending.length === 1 ? "suggestion" : "suggestions"}`;
    details.append(summary);
    pending.forEach((proposal) => {
      const row = document.createElement("div");
      row.className = "anime-mapping-proposal";
      const copy = document.createElement("div");
      const id = proposalReleaseGroupId(proposal);
      const title = document.createElement("strong");
      title.textContent = String(
        proposal.releaseGroupTitle
        || proposal.release_group_title
        || proposal.releaseGroup?.releaseGroupTitle
        || proposal.releaseGroup?.title
        || id
        || "Suggested release group",
      );
      const submittedBy = document.createElement("span");
      submittedBy.textContent = String(
        proposal.username
        || proposal.submittedBy?.username
        || proposal.submittedBy?.displayName
        || proposal.requester?.username
        || proposal.user?.username
        || "Melodarr user",
      );
      copy.append(title, submittedBy);
      const actions = document.createElement("div");
      actions.className = "anime-mapping-proposal-actions";
      if (id || proposal.musicBrainzUrl) {
        const musicBrainz = document.createElement("a");
        musicBrainz.href = String(
          proposal.musicBrainzUrl
          || `https://musicbrainz.org/release-group/${encodeURIComponent(id)}`,
        );
        musicBrainz.target = "_blank";
        musicBrainz.rel = "noreferrer";
        musicBrainz.textContent = "MusicBrainz";
        actions.append(musicBrainz);
      }
      const approve = document.createElement("button");
      approve.type = "button";
      approve.textContent = "Approve";
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "anime-mapping-unlink";
      reject.textContent = "Reject";
      const proposalEndpoint = `/api/anime/${encodeURIComponent(String(currentDetail?.id || ""))}`
        + `/themes/${encodeURIComponent(String(theme.id))}`
        + `/mapping-proposals/${encodeURIComponent(String(proposal.id || ""))}`;
      const finish = async (approved: boolean) => {
        approve.disabled = true;
        reject.disabled = true;
        try {
          const payload = approved
            ? await postJson(`${proposalEndpoint}/approve`, {})
            : await api(proposalEndpoint, { method: "DELETE" });
          if (payload.proposals === undefined) {
            theme.proposals = proposals.filter((candidate) => candidate !== proposal);
          }
          updateAnimeThemeMapping(theme, payload);
        } catch (error) {
          approve.disabled = false;
          reject.disabled = false;
          showToast(error.message, true);
        }
      };
      approve.addEventListener("click", () => finish(true));
      reject.addEventListener("click", () => finish(false));
      actions.append(approve, reject);
      row.append(copy, actions);
      details.append(row);
    });
    return details;
  }

  function createAnimeMapping(theme: JsonObject, unavailable = false, manageMappings = false) {
    const mapping = theme.mapping as JsonObject | undefined;
    const state = animeMappingState(mapping, unavailable);
    const container = document.createElement("div");
    container.className = `anime-theme-mapping anime-mapping-${state}`;
    const status = document.createElement("span");
    status.className = "anime-mapping-status";
    status.setAttribute("role", "status");
    status.textContent = state === "resolved"
      ? "Matched"
      : state === "ambiguous"
        ? "Choose a release"
        : state === "unmatched"
          ? "No MusicBrainz match"
          : state === "failed"
            ? "Matching failed"
            : state === "unavailable" ? "Not mapped yet" : "Matching with MusicBrainz…";
    const summary = document.createElement("div");
    summary.className = "anime-mapping-summary";
    const provenance = document.createElement("span");
    provenance.className = "anime-mapping-provenance";
    provenance.textContent = animeMappingProvenance(mapping, state);
    summary.append(status, provenance);
    container.append(summary);

    if (mapping?.recordingId) {
      const recording = document.createElement("a");
      recording.className = "anime-recording-link";
      recording.href = `https://musicbrainz.org/recording/${encodeURIComponent(mapping.recordingId)}`;
      recording.target = "_blank";
      recording.rel = "noreferrer";
      recording.textContent = mapping.recordingTitle
        ? `Recording: ${mapping.recordingTitle}`
        : "Open MusicBrainz recording";
      container.append(recording);
    }

    const directGroups = (mapping?.releaseGroups || mapping?.candidates || []) as JsonObject[];
    const recordingGroups = (mapping?.recordingCandidates || [])
      .flatMap((candidate: JsonObject) => candidate.releaseGroups || []) as JsonObject[];
    const groups = [...directGroups, ...recordingGroups].filter(
      (group, index, all) => group?.id && all.findIndex((candidate) => candidate?.id === group.id) === index,
    );
    const automaticMatchMethod = String(mapping?.matchMethod || "");
    const canConfirmAmbiguousCandidate = currentUser?.role === "admin"
      && manageMappings
      && state === "ambiguous"
      && groups.length > 0
      && !mapping?.mappingSource;
    const confirmAutomaticMatch = async (
      releaseGroupId: string,
      button: HTMLButtonElement,
      failureLabel: string,
    ) => {
      button.disabled = true;
      button.textContent = "Confirming…";
      const endpoint = `/api/anime/${encodeURIComponent(String(currentDetail?.id || ""))}`
        + `/themes/${encodeURIComponent(String(theme.id))}/mapping`;
      try {
        const payload = await api(endpoint, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            confirmAutomatic: true,
            releaseGroup: releaseGroupId,
          }),
        });
        updateAnimeThemeMapping(theme, payload);
      } catch (error) {
        button.disabled = false;
        button.textContent = failureLabel;
        showToast(error.message, true);
      }
    };
    let recommendedId = "";
    if (groups.length) {
      const candidateDetails = document.createElement("details");
      candidateDetails.className = "anime-mapping-candidates";
      candidateDetails.open = true;
      const candidateSummary = document.createElement("summary");
      candidateSummary.textContent = `${groups.length} release ${groups.length === 1 ? "option" : "options"}`;
      const candidates = document.createElement("div");
      candidates.className = "anime-release-candidates";
      recommendedId = String(
        mapping?.recommendedReleaseGroupId
        || mapping?.recommended?.id
        || (state === "resolved" ? groups[0]?.id : ""),
      );
      const renderCandidateLimit = (showAll: boolean) => {
        const visible = showAll ? groups : groups.slice(0, 3);
        candidates.replaceChildren(...visible.map((group) => createAnimeReleaseCandidate(
          group,
          theme,
          Boolean(recommendedId) && String(group.id) === recommendedId,
          canConfirmAmbiguousCandidate
            ? (button) => confirmAutomaticMatch(String(group.id), button, "Confirm match")
            : undefined,
        )));
        if (groups.length > 3) {
          const toggle = document.createElement("button");
          toggle.className = "secondary-action anime-candidates-toggle";
          toggle.type = "button";
          toggle.textContent = showAll ? "Show fewer" : `Show all ${groups.length}`;
          toggle.addEventListener("click", () => {
            renderCandidateLimit(!showAll);
            if (showAll) {
              window.requestAnimationFrame(() => {
                const scrollTarget = candidateDetails.closest<HTMLElement>(".anime-theme-card")
                  || candidateDetails;
                const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                scrollTarget.scrollIntoView({
                  behavior: reduceMotion ? "auto" : "smooth",
                  block: "start",
                });
              });
            }
          });
          candidates.append(toggle);
        }
      };
      renderCandidateLimit(false);
      candidateDetails.append(candidateSummary, candidates);
      container.append(candidateDetails);
    }
    const canConfirmAutomatic = currentUser?.role === "admin"
      && manageMappings
      && state === "resolved"
      && (
        automaticMatchMethod === "recording-search"
        || automaticMatchMethod === "artist-discography-title"
      )
      && (
        automaticMatchMethod !== "recording-search"
        || Boolean(mapping?.recordingId)
      )
      && Boolean(recommendedId)
      && !mapping?.mappingSource;
    if (canConfirmAutomatic) {
      const confirmation = document.createElement("div");
      confirmation.className = "anime-mapping-confirmation";
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.textContent = "Confirm recommended match";
      const confirmationMessage = document.createElement("span");
      confirmationMessage.setAttribute("role", "status");
      confirm.addEventListener("click", async () => {
        confirm.disabled = true;
        confirmationMessage.classList.remove("error");
        confirmationMessage.textContent = "Saving recommended MusicBrainz mapping…";
        const endpoint = `/api/anime/${encodeURIComponent(String(currentDetail?.id || ""))}`
          + `/themes/${encodeURIComponent(String(theme.id))}/mapping`;
        try {
          const payload = await api(endpoint, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              confirmAutomatic: true,
              releaseGroup: recommendedId,
            }),
          });
          updateAnimeThemeMapping(theme, payload);
        } catch (error) {
          confirmationMessage.classList.add("error");
          confirmationMessage.textContent = error.message;
          confirm.disabled = false;
        }
      });
      confirmation.append(confirm, confirmationMessage);
      container.append(confirmation);
    }
    const proposalReview = currentUser?.role === "admin" && manageMappings
      ? createAnimeProposalReview(theme, mapping)
      : null;
    if (proposalReview) container.append(proposalReview);
    const editor = createAnimeMappingEditor(theme, mapping, manageMappings);
    if (editor) container.append(editor);
    return container;
  }

  function createAnimeThemeCard(
    theme: JsonObject,
    unavailable = false,
    manageMappings = false,
  ) {
    const card = document.createElement("article");
    card.className = "anime-theme-card";
    if (theme.id !== undefined && theme.id !== null && String(theme.id)) {
      card.id = `theme-${theme.id}`;
      card.dataset.themeId = String(theme.id);
    }
    const heading = document.createElement("div");
    heading.className = "anime-theme-heading";
    const sequence = document.createElement("span");
    sequence.className = "anime-theme-sequence";
    sequence.textContent = animeThemeLabel(theme);
    const title = document.createElement("h3");
    title.textContent = String(theme.song?.title || "Untitled theme");
    const artists = document.createElement("p");
    artists.className = "anime-theme-artists";
    artists.textContent = animeArtistNames(theme.song || {}) || "Unknown artist";
    heading.append(sequence, title, artists);
    const notes = Array.isArray(theme.notes)
      ? theme.notes.filter(Boolean).join("; ")
      : String(theme.notes || "").trim();
    const facts = [animeEpisodes(theme.episodes), notes].filter(Boolean);
    if (facts.length) {
      const metadata = document.createElement("p");
      metadata.className = "anime-theme-facts";
      metadata.textContent = facts.join(" · ");
      heading.append(metadata);
    }
    card.append(heading, createAnimeMapping(theme, unavailable, manageMappings));
    return card;
  }

  function animeHashThemeId() {
    const encoded = window.location.hash.match(/^#theme-(.+)$/)?.[1];
    if (!encoded) return "";
    try {
      return decodeURIComponent(encoded);
    } catch {
      // A hand-edited malformed fragment should not prevent the anime page
      // from rendering; it simply cannot target a particular theme.
      return "";
    }
  }

  function animeResourceEntries(resources: unknown) {
    if (Array.isArray(resources)) return resources as JsonObject[];
    if (!resources || typeof resources !== "object") return [];
    return Object.entries(resources as JsonObject).map(([name, value]) => (
      typeof value === "string" ? { name, url: value } : { name, ...value }
    ));
  }

  function safeAnimeResourceUrl(resource: JsonObject) {
    const value = String(resource.url || resource.link || "");
    try {
      const url = new URL(value);
      const hostname = url.hostname.toLowerCase();
      const isMyAnimeList = hostname === "myanimelist.net"
        || hostname.endsWith(".myanimelist.net");
      return url.protocol === "https:" && isMyAnimeList
        ? url.href
        : "";
    } catch {
      return "";
    }
  }

  function animeSeriesEntries(series: unknown) {
    const entries = Array.isArray(series) ? series : series ? [series] : [];
    return entries.map((entry) => typeof entry === "string" ? { name: entry } : entry)
      .filter((entry) => entry && (entry.name || entry.title));
  }

  function appendAnimeSeriesLinks(container: Element, series: unknown) {
    const entries = animeSeriesEntries(series);
    if (!entries.length) return;
    const seriesCopy = document.createElement("p");
    seriesCopy.className = "anime-series-links";
    seriesCopy.append("Series: ");
    entries.forEach((entry: JsonObject, index: number) => {
      if (index) seriesCopy.append(", ");
      const name = String(entry.name || entry.title);
      const slug = String(entry.slug || "");
      if (!slug) {
        seriesCopy.append(name);
        return;
      }
      const link = document.createElement("a");
      link.href = detailPath("series", slug);
      link.textContent = name;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showDetail("series", slug);
      });
      addDetailPrefetch(link, "series", slug);
      seriesCopy.append(link);
    });
    container.append(seriesCopy);
  }

  function animeExternalLinks(data: JsonObject) {
    const links = animeResourceEntries(data.resources)
      .map((resource) => ({ resource, url: safeAnimeResourceUrl(resource) }))
      .filter(({ resource, url }) => (
        url && String(resource.name || resource.site || "").toLowerCase() === "myanimelist"
      ))
      .map(({ url }) => ({ label: "MyAnimeList", url }));
    const slug = String(data.slug || "");
    if (slug) {
      links.push({
        label: "AnimeThemes",
        url: `https://animethemes.moe/anime/${encodeURIComponent(slug)}`,
      });
    }
    return links.filter((link, index, all) => (
      all.findIndex((candidate) => candidate.url === link.url) === index
    ));
  }

  function appendAnimeExternalLinks(container: Element, links: Array<{ label: string; url: string }>) {
    if (!links.length) return;
    const wrapper = document.createElement("div");
    wrapper.className = "anime-resource-links";
    links.forEach(({ label, url }) => {
      const provider = label === "MyAnimeList" ? "myanimelist" : "animethemes";
      wrapper.append(createServiceIconLink(
        url,
        `/icons/${provider}.svg`,
        `Open on ${label}`,
        `service-icon-link anime-resource-icon anime-resource-${provider}`,
      ));
    });
    container.append(wrapper);
  }

  function renderAnimeDetail(data: JsonObject) {
    const results = $("#detail-results");
    results.replaceChildren();
    $("#detail-eyebrow").textContent = "ANIME THEMES";
    $("#detail-title").textContent = String(data.name || "Anime");
    $("#detail-subtitle").textContent = [data.format, data.season, data.year]
      .filter(Boolean)
      .join(" · ");
    if (data.coverArt) loadDetailCover(data.coverArt, `Cover art for ${data.name}`);
    else resetDetailCover();

    const meta = document.createElement("section");
    meta.className = "artist-meta anime-meta";
    const facts = document.createElement("strong");
    facts.textContent = `AnimeThemes ID: ${data.id}`;
    meta.append(facts);
    appendAnimeSeriesLinks(meta, data.series);
    appendAnimeExternalLinks(meta, animeExternalLinks(data));
    results.append(meta);

    const slug = String(data.slug || currentDetail?.id || "");
    let ui = animeDetailUi.get(slug);
    if (!ui) {
      ui = { initialized: false, manageMappings: false, openSections: new Set() };
      animeDetailUi.set(slug, ui);
    }
    if (currentUser?.role === "admin") {
      const controls = document.createElement("div");
      controls.className = "anime-theme-controls";
      const manage = document.createElement("button");
      manage.className = "secondary-action anime-manage-mappings";
      manage.type = "button";
      manage.setAttribute("aria-pressed", String(ui.manageMappings));
      manage.textContent = ui.manageMappings ? "Done managing mappings" : "Manage mappings";
      manage.addEventListener("click", () => {
        ui!.manageMappings = !ui!.manageMappings;
        renderAnimeDetail(data);
      });
      controls.append(manage);
      results.append(controls);
    }

    const grouped: Record<string, Array<{ theme: JsonObject; index: number }>> = {
      opening: [], ending: [], other: [],
    };
    (data.themes || []).forEach((theme: JsonObject, index: number) => {
      grouped[animeThemeKind(theme)].push({ theme, index });
    });
    const sequenceOrder = (first: { theme: JsonObject; index: number }, second: { theme: JsonObject; index: number }) => {
      const firstSequence = Number(first.theme.sequence);
      const secondSequence = Number(second.theme.sequence);
      if (Number.isFinite(firstSequence) && Number.isFinite(secondSequence) && firstSequence !== secondSequence) {
        return firstSequence - secondSequence;
      }
      return first.index - second.index;
    };
    const sectionLabels: Record<string, string> = {
      opening: "Openings", ending: "Endings", other: "Other themes",
    };
    const orderedSections = Object.entries(grouped)
      .filter(([, themes]) => themes.length);
    const hashThemeId = animeHashThemeId();
    const hashSection = hashThemeId
      ? orderedSections.find(([, themes]) => themes.some(({ theme }) => String(theme.id) === hashThemeId))?.[0]
      : undefined;
    if (!ui.initialized) {
      const defaultSection = hashSection || orderedSections[0]?.[0];
      if (defaultSection) ui.openSections.add(defaultSection);
      ui.initialized = true;
    } else if (hashSection) {
      ui.openSections.add(hashSection);
    }

    const layout = document.createElement("div");
    layout.className = "discography-layout anime-theme-layout";
    const index = document.createElement("nav");
    index.className = "discography-nav anime-theme-nav";
    index.setAttribute("aria-label", "Anime theme sections");
    const content = document.createElement("div");
    content.className = "discography-content anime-theme-content";

    orderedSections.forEach(([kind, themes]) => {
      if (!themes.length) return;
      themes.sort(sequenceOrder);
      const section = document.createElement("details");
      section.id = `anime-theme-${kind}`;
      section.className = "discography-section anime-theme-section";
      section.open = ui!.openSections.has(kind);
      const summary = document.createElement("summary");
      summary.textContent = `${sectionLabels[kind]} (${themes.length})`;
      section.append(summary);
      let rendered = false;
      const renderSection = () => {
        if (!section.open) {
          rendered = false;
          section.replaceChildren(summary);
          return;
        }
        if (!rendered) {
          section.replaceChildren(summary, ...themes.map(({ theme }) => createAnimeThemeCard(
            theme,
            Boolean(theme.resolutionUnavailable || data.resolutionUnavailable),
            ui!.manageMappings,
          )));
          rendered = true;
        }
        observeAnimeThemeCards();
      };
      section.addEventListener("toggle", () => {
        if (section.open) ui!.openSections.add(kind);
        else ui!.openSections.delete(kind);
        renderSection();
      });
      renderSection();
      content.append(section);

      const link = document.createElement("a");
      link.href = `#${section.id}`;
      link.textContent = sectionLabels[kind];
      link.addEventListener("click", (event) => {
        event.preventDefault();
        section.open = true;
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      index.append(link);
    });
    if (orderedSections.length) {
      layout.append(index, content);
      results.append(layout);
    }
    if (!(data.themes || []).length) {
      const empty = document.createElement("p");
      empty.className = "message";
      empty.textContent = "No opening or ending themes are listed for this anime yet.";
      results.append(empty);
    }
    observeAnimeThemeCards();
    if (hashThemeId) {
      window.requestAnimationFrame(() => {
        const target = document.getElementById(`theme-${hashThemeId}`);
        target?.scrollIntoView({ block: "start" });
      });
    }
  }

  function renderAnimeSeriesDetail(data: JsonObject) {
    const results = $("#detail-results");
    results.replaceChildren();
    resetDetailCover();
    $("#detail-eyebrow").textContent = "ANIME SERIES";
    $("#detail-title").textContent = String(data.name || "Anime series");
    const anime = Array.isArray(data.anime) ? data.anime : [];
    $("#detail-subtitle").textContent = `${anime.length} related anime`;

    const meta = document.createElement("section");
    meta.className = "artist-meta anime-meta anime-series-meta";
    const copy = document.createElement("p");
    copy.textContent = "Related productions and their opening and ending catalogs.";
    meta.append(copy);
    const slug = String(data.slug || currentDetail?.id || "");
    if (slug) {
      appendAnimeExternalLinks(meta, [{
        label: "AnimeThemes",
        url: `https://animethemes.moe/series/${encodeURIComponent(slug)}`,
      }]);
    }
    results.append(meta);

    if (!anime.length) {
      const empty = document.createElement("p");
      empty.className = "message";
      empty.textContent = "No related anime are listed for this series yet.";
      results.append(empty);
      return;
    }

    const grid = document.createElement("section");
    grid.className = "anime-series-grid";
    anime.forEach((entry: JsonObject) => {
      const animeSlug = String(entry.slug || "");
      if (!animeSlug) return;
      const name = String(entry.name || "Anime");
      const link = document.createElement("a");
      link.className = "anime-series-card";
      link.href = detailPath("anime", animeSlug);
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showDetail("anime", animeSlug);
      });
      addDetailPrefetch(link, "anime", animeSlug);

      const artwork = document.createElement("div");
      artwork.className = "anime-series-artwork";
      const fallback = document.createElement("div");
      fallback.className = "anime-series-artwork-fallback";
      fallback.textContent = name.slice(0, 1).toUpperCase();
      if (entry.coverArt) {
        const image = document.createElement("img");
        image.alt = `Cover art for ${name}`;
        image.decoding = "async";
        loadArtworkWhenNear(image, String(entry.coverArt), fallback);
        artwork.append(image);
      } else {
        artwork.append(fallback);
      }

      const info = document.createElement("div");
      info.className = "anime-series-card-info";
      const heading = document.createElement("h2");
      heading.textContent = name;
      const metadata = document.createElement("p");
      metadata.textContent = [entry.format, entry.season, entry.year]
        .filter(Boolean)
        .join(" · ");
      const themeCount = Number(entry.themeCount || 0);
      const themes = document.createElement("span");
      themes.textContent = `${themeCount} ${themeCount === 1 ? "theme" : "themes"}`;
      info.append(heading, metadata, themes);
      link.append(artwork, info);
      grid.append(link);
    });
    results.append(grid);
  }

  function animeAssociationLabel(association: JsonObject) {
    if (association.themeLabel) return String(association.themeLabel);
    const kind = String(association.themeType || "").toLowerCase();
    const prefix = kind === "op" || kind.startsWith("open")
      ? "Opening"
      : kind === "ed" || kind.startsWith("end") ? "Ending" : "Theme";
    return association.sequence === undefined || association.sequence === null
      ? prefix
      : `${prefix} ${association.sequence}`;
  }

  function animeAssociationPath(association: JsonObject) {
    const slug = String(association.animeSlug || "");
    const suppliedPath = String(association.animePath || "").split("#", 1)[0];
    const base = suppliedPath.startsWith("/anime/")
      ? suppliedPath
      : slug ? detailPath("anime", slug) : "";
    const themeId = String(association.themeId || "");
    return base && themeId ? `${base}#theme-${encodeURIComponent(themeId)}` : base;
  }

  function releaseAnimeRequestContext(data: JsonObject) {
    const associations = (data.animeThemes || []) as JsonObject[];
    const association = associations
      .map((entry, providerIndex) => ({
        entry,
        providerIndex,
        specificity: [
          entry.animeSlug,
          entry.animeName,
          entry.themeId,
          entry.themeLabel,
          entry.songId,
          entry.songTitle,
        ].filter(Boolean).length,
      }))
      .sort((first, second) => (
        second.specificity - first.specificity
        || first.providerIndex - second.providerIndex
      ))[0]?.entry;
    if (!association?.animeSlug || !association?.animeName || !association?.themeId) {
      return undefined;
    }
    return {
      animeSlug: String(association.animeSlug),
      animeName: String(association.animeName),
      themeId: String(association.themeId),
      themeLabel: animeAssociationLabel(association),
      songId: String(association.songId || ""),
      songTitle: String(association.songTitle || ""),
    };
  }

  function createReleaseAnimeThemes(associations: JsonObject[]) {
    const section = document.createElement("section");
    section.className = "release-anime-themes";
    const heading = document.createElement("h2");
    heading.textContent = "Featured in anime";
    const list = document.createElement("div");
    list.className = "release-anime-theme-list";
    associations.forEach((association) => {
      const path = animeAssociationPath(association);
      if (!path) return;
      const link = document.createElement("a");
      link.className = "release-anime-theme-link";
      link.href = path;
      const copy = document.createElement("span");
      const anime = document.createElement("strong");
      anime.textContent = String(association.animeName || "Anime");
      const theme = document.createElement("span");
      theme.textContent = [animeAssociationLabel(association), association.songTitle]
        .filter(Boolean)
        .join(" · ");
      copy.append(anime, theme);
      const arrow = document.createElement("span");
      arrow.className = "release-anime-theme-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "→";
      link.append(copy, arrow);
      const slug = String(association.animeSlug || "");
      if (slug) {
        link.addEventListener("click", (event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          showDetail("anime", slug, true, false);
          window.history.pushState(
            detailNavigationState("anime", slug),
            "",
            path,
          );
        });
      }
      list.append(link);
    });
    section.append(heading, list);
    return section;
  }

  function stopAnimeResolution() {
    if (animeResolutionWatcher?.timer) clearTimeout(animeResolutionWatcher.timer);
    animeResolutionWatcher?.observer?.disconnect();
    animeResolutionWatcher = undefined;
  }

  function applyAnimeResolution(watcher: AnimeResolutionWatcher, payload: JsonObject) {
    const mappings = payload.mappings || {};
    let changed = false;
    (watcher.data.themes || []).forEach((theme: JsonObject) => {
      const mapping = mappings[String(theme.id)];
      if (!mapping) return;
      if (JSON.stringify(theme.mapping) !== JSON.stringify(mapping)) {
        theme.mapping = mapping;
        theme.resolutionUnavailable = false;
        changed = true;
      }
    });
    if (changed && currentDetail?.kind === "anime" && currentDetail.id === watcher.slug) {
      watcher.data.resolutionUnavailable = false;
      renderAnimeDetail(watcher.data);
    }
    const status = String(payload.status || "").toLowerCase();
    // Progressive responses describe every theme, so the aggregate status can
    // remain "pending" simply because a collapsed section has not been
    // requested yet. When the backend supplies its explicit queue flag, use it
    // as the authority and avoid polling for those intentionally deferred rows.
    if (payload.polling !== undefined) return payload.polling === true;
    if (["pending", "queued", "running", "resolving"].includes(status)) return true;
    return false;
  }

  function markAnimeResolutionUnavailable(watcher: AnimeResolutionWatcher) {
    (watcher.data.themes || []).forEach((theme: JsonObject) => {
      if (
        watcher.requestedThemeIds.has(String(theme.id))
        && animeMappingState(theme.mapping) === "pending"
      ) {
        theme.resolutionUnavailable = true;
      }
    });
    if (currentDetail?.kind === "anime" && currentDetail.id === watcher.slug) {
      renderAnimeDetail(watcher.data);
    }
  }

  function scheduleAnimeResolutionPoll(watcher: AnimeResolutionWatcher, delay = 1_000) {
    if (animeResolutionWatcher !== watcher || watcher.timer) return;
    watcher.timer = setTimeout(() => {
      watcher.timer = undefined;
      pollAnimeResolution(watcher);
    }, delay);
  }

  async function pollAnimeResolution(watcher: AnimeResolutionWatcher) {
    if (animeResolutionWatcher !== watcher) return;
    watcher.attempts += 1;
    try {
      const payload = await getJson(
        `/api/anime/${encodeURIComponent(watcher.slug)}/resolution`,
        30_000,
      );
      if (animeResolutionWatcher !== watcher) return;
      const polling = applyAnimeResolution(watcher, payload);
      if (polling && watcher.attempts < watcher.maxAttempts) {
        scheduleAnimeResolutionPoll(watcher, 2_000);
      } else if (polling) {
        markAnimeResolutionUnavailable(watcher);
      }
    } catch {
      if (animeResolutionWatcher === watcher) markAnimeResolutionUnavailable(watcher);
    }
  }

  function requestAnimeThemeResolution(watcher: AnimeResolutionWatcher, themeId: string) {
    if (animeResolutionWatcher !== watcher || watcher.requestedThemeIds.has(themeId)) return;
    watcher.requestedThemeIds.add(themeId);
    const theme = (watcher.data.themes || [])
      .find((candidate: JsonObject) => String(candidate.id) === themeId);
    if (theme) theme.resolutionUnavailable = false;
    watcher.attempts = 0;
    postJson(`/api/anime/${encodeURIComponent(watcher.slug)}/resolve`, {
      themeIds: [themeId],
    })
      .then((payload) => {
        if (animeResolutionWatcher !== watcher) return;
        const polling = applyAnimeResolution(watcher, payload);
        if (polling) scheduleAnimeResolutionPoll(watcher);
      })
      .catch(() => {
        if (animeResolutionWatcher !== watcher) return;
        if (theme) theme.resolutionUnavailable = true;
        renderAnimeDetail(watcher.data);
      });
  }

  function observeAnimeThemeCards() {
    const watcher = animeResolutionWatcher;
    if (!watcher || currentDetail?.kind !== "anime" || currentDetail.id !== watcher.slug) return;
    watcher.observer?.disconnect();
    const cards = [...document.querySelectorAll<HTMLElement>(".anime-theme-card[data-theme-id]")]
      .filter((card) => !watcher.requestedThemeIds.has(String(card.dataset.themeId || "")));
    if (!("IntersectionObserver" in window)) {
      cards.forEach((card) => requestAnimeThemeResolution(
        watcher,
        String(card.dataset.themeId || ""),
      ));
      return;
    }
    watcher.observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        watcher.observer?.unobserve(entry.target);
        const themeId = String((entry.target as HTMLElement).dataset.themeId || "");
        if (themeId) requestAnimeThemeResolution(watcher, themeId);
      });
    }, { rootMargin: "350px 0px" });
    cards.forEach((card) => watcher.observer?.observe(card));
  }

  function startAnimeResolution(data: JsonObject) {
    stopAnimeResolution();
    const slug = String(data.slug || currentDetail?.id || "");
    if (!slug || !(data.themes || []).length) return;
    const themeCount = (data.themes || []).length;
    const watcher: AnimeResolutionWatcher = {
      slug,
      data,
      attempts: 0,
      // MusicBrainz is intentionally rate-limited. Larger anime catalogs need
      // a longer polling window so their background mappings can finish.
      maxAttempts: Math.max(30, Math.min(300, themeCount * 4)),
      requestedThemeIds: new Set(
        (data.themes || [])
          .filter((theme: JsonObject) => (
            theme.mapping && animeMappingState(theme.mapping) !== "pending"
          ))
          .map((theme: JsonObject) => String(theme.id)),
      ),
    };
    animeResolutionWatcher = watcher;
    observeAnimeThemeCards();
  }

  function renderDetail(kind: DetailKind, data: JsonObject) {
    const results = $("#detail-results");
    currentDetailData = data;
    results.replaceChildren();
    $("#detail-message").textContent = "";

    if (kind === "anime") {
      renderAnimeDetail(data);
      startAnimeResolution(data);
      return;
    }

    if (kind === "series") {
      renderAnimeSeriesDetail(data);
      return;
    }

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
      requestButton.className = "request-artist detail-availability-action";
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
      startDetailAvailability("artist", data);
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
      const animeThemes = (data.animeThemes || []) as JsonObject[];
      if (animeThemes.length) results.append(createReleaseAnimeThemes(animeThemes));
      const requestButton = document.createElement("button");
      requestButton.className = "request-artist detail-availability-action";
      if (data.fullyAvailableInLidarr) {
        requestButton.textContent = "Available";
        requestButton.disabled = true;
        requestButton.title = "This release group is fully available in Lidarr";
      } else {
        requestButton.textContent = data.availableInLidarr
          ? "Search missing"
          : "Request release group";
        requestButton.addEventListener("click", () => requestReleaseGroup({
          id: data.id,
          button: requestButton,
          animeContext: releaseAnimeRequestContext(data),
        }));
      }
      results.append(requestButton);
      data.releases.forEach((release: JsonObject) => {
        const card = createCard(
          releaseGroupDisplayTitle(release),
          [release.date, release.country, release.format, release.trackCount ? `${release.trackCount} tracks` : "", release.status, release.disambiguation].filter(Boolean).join(" · "),
          () => showDetail("release", release.id),
        );
        card.dataset.releaseId = String(release.id);
        if (release.availableInPlex) addPlexAvailability(card, "This edition is in Plex");
        results.append(card);
      });
      startDetailAvailability("release-group", data);
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
    anime: { placeholder: "Search anime titles…", noun: "anime title" },
  } as const;

  function copyForSearchType(type: string) {
    return searchTypeCopy[type as keyof typeof searchTypeCopy] || searchTypeCopy.artist;
  }

  const searchType = $<HTMLSelectElement>("#search-type");
  const searchInput = $<HTMLInputElement>("#search-input");
  const searchSubmit = $<HTMLButtonElement>("#search-submit");
  let activeSearchType = searchType.value;
  let searchTypePointerActive = false;

  function updateSearchSubmitState() {
    searchSubmit.disabled = searchInput.value.trim().length < 2;
  }

  function applySearchMode(type: string) {
    searchInput.placeholder = copyForSearchType(type).placeholder;
    searchInput.setAttribute(
      "aria-label",
      type === "anime" ? "Search anime titles" : `Search ${type}s`,
    );
    searchInput.removeAttribute("maxlength");
    searchSubmit.textContent = "Search";
    updateSearchSubmitState();
  }

  applySearchMode(activeSearchType);

  function searchResultMessage(type: string, count: number) {
    const noun = copyForSearchType(type).noun;
    const summary = `${count} ${noun}${count === 1 ? "" : "s"} found`;
    return type === "track" ? `${summary} for matching tracks` : summary;
  }

  function animeSearchResultsByFormat(results: JsonObject[]) {
    const formatPriority = (format: unknown) => {
      const normalized = String(format || "").trim().toLowerCase();
      if (normalized === "tv" || normalized.startsWith("tv ")) return 0;
      if (normalized === "movie" || normalized.includes("film")) return 1;
      if (normalized === "ova") return 2;
      if (normalized === "ona") return 3;
      if (normalized === "special") return 4;
      return 5;
    };
    return results
      .map((result, providerIndex) => ({ result, providerIndex }))
      .sort((first, second) => (
        formatPriority(first.result.format) - formatPriority(second.result.format)
        || first.providerIndex - second.providerIndex
      ))
      .map(({ result }) => result);
  }

  searchType.addEventListener("pointerdown", () => {
    searchTypePointerActive = true;
  });
  searchType.addEventListener("keydown", () => {
    searchTypePointerActive = false;
  });
  searchType.addEventListener("blur", () => {
    searchTypePointerActive = false;
  });

  searchType.addEventListener("change", (event) => {
    const type = (event.target as HTMLSelectElement).value;
    activeSearchType = type;
    searchRequestVersion += 1;
    searchAbort?.abort();
    searchAbort = undefined;
    clearTimeout(searchDebounce);
    applySearchMode(type);
    if (searchInput.value.trim().length >= 2) {
      runSearch();
    }
    if (searchTypePointerActive) searchType.blur();
  });

  async function runSearch() {
    const requestVersion = ++searchRequestVersion;
    searchAbort?.abort();
    const query = searchInput.value.trim();
    const type = searchType.value;
    const results = $("#results");
    results.setAttribute("aria-label", "Search results");

    if (query.length < 2) {
      searchAbort = undefined;
      $("#search-form").classList.remove("searching");
      results.replaceChildren();
      $("#search-message").textContent = "";
      return;
    }

    const controller = new AbortController();
    searchAbort = controller;
    $("#search-message").textContent = type === "anime"
      ? "Looking through anime themes…"
      : "Looking through MusicBrainz…";
    $("#search-form").classList.add("searching");
    results.setAttribute("aria-busy", "true");
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
        : "We couldn’t find a match. Try a different spelling or search type.";
      const orderedResults = type === "anime"
        ? animeSearchResultsByFormat(data.results)
        : data.results;
      orderedResults.forEach((result: JsonObject) => {
        const description = type === "anime"
          ? [result.format, result.season, result.year].filter(Boolean).join(" · ")
          : type === "artist"
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
        if (type === "anime") {
          results.append(createCard(
            String(result.name || "Anime"),
            description,
            () => showDetail("anime", String(result.slug || result.id)),
            result.coverArt,
            "anime",
            String(result.slug || result.id),
          ));
        } else {
          results.append(type === "artist"
            ? (result.plex ? createPlexArtistCard(result, description, result.plex) : createSearchArtistCard(result, description))
            : createCard(releaseGroupDisplayTitle(result), description, () => showDetail("release-group", result.id)));
        }
      });
    } catch (error) {
      if (requestVersion !== searchRequestVersion) return;
      results.replaceChildren();
      $("#search-message").textContent = error.name === "AbortError"
        ? `${type === "anime" ? "Anime theme search" : "MusicBrainz"} is taking a little longer than usual. Please try again in a moment.`
        : `We couldn’t finish that search. ${error.message}`;
    } finally {
      if (requestVersion === searchRequestVersion) {
        searchAbort = undefined;
        $("#search-form").classList.remove("searching");
        results.removeAttribute("aria-busy");
      }
    }
  }

  // MusicBrainz requests are serialized at roughly one per second upstream, so
  // this waits for a genuine pause in typing rather than firing per keystroke.
  const searchDebounceMilliseconds = 450;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    updateSearchSubmitState();
    searchDebounce = setTimeout(runSearch, searchDebounceMilliseconds);
  });

  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(searchDebounce);
    runSearch();
  });

  async function loadRecommendations(button: HTMLButtonElement) {
    const requestVersion = ++recommendationRequestVersion;
    recommendationAbort?.abort();
    const controller = new AbortController();
    recommendationAbort = controller;
    clearTimeout(recommendationPoll);
    const message = $("#recommendations-message");
    const results = $("#recommendation-results");
    button.disabled = true;
    results.setAttribute("aria-busy", "true");
    const placeholder = document.createElement("div");
    placeholder.className = "recommendation-carousel";
    placeholder.append(skeletonBlock("skeleton-art", 8));
    results.replaceChildren(placeholder);
    message.textContent = "Gathering a few recommendations for you…";
    try {
      const data = await getJson("/api/discover", 30_000, controller.signal);
      if (requestVersion !== recommendationRequestVersion) return;
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
      if (["partial", "unavailable"].includes(providerStatus.plexHistory)) unavailableProviders.push("Plex history");
      const retryNotice = unavailableProviders.length
        ? ` ${unavailableProviders.join(" and ")} was temporarily unavailable; available results are shown and a retry is scheduled.`
        : "";
      message.textContent = `Last refreshed ${new Date(data.refreshedAt * 1000).toLocaleString()}.${retryNotice}`;
      if (artists.length) results.append(recommendationRow("Artists", artists, "artist"));
      if (otherReleases.length) results.append(recommendationRow("Albums", otherReleases, "release-group"));
      if (singles.length) results.append(recommendationRow("Singles", singles, "release-group"));
      if (data.chartArtists?.length) results.append(recommendationRow("Popular on Last.fm", data.chartArtists, "artist"));
      const tagRows = (data.tagRows || []).filter((row: JsonObject) => row.albums?.length);
      if (tagRows.length) results.append(deferredTasteRows(tagRows));
      if (!artists.length && !albums.length && !data.chartArtists?.length && !tagRows.length && !unavailableProviders.length) {
        message.textContent = "We don’t have a recommendation match yet. A little more listening history will help.";
      }
    } catch (error) {
      if (requestVersion !== recommendationRequestVersion) return;
      results.replaceChildren();
      message.textContent = error.name === "AbortError"
        ? "Recommendations are taking a little longer than usual. Please try again in a moment."
        : `We couldn’t load recommendations just now. ${error.message}`;
    } finally {
      if (requestVersion === recommendationRequestVersion) {
        recommendationAbort = undefined;
        button.disabled = false;
        results.removeAttribute("aria-busy");
      }
    }
  }

  $("#load-recommendations").addEventListener("click", () => {
    loadRecommendations($("#load-recommendations"));
  });
  window.addEventListener("melodarr-authenticated", () => {
    loadRecommendations($("#load-recommendations"));
  });
  window.addEventListener("melodarr-recommendations-changed", () => loadRecommendations($("#load-recommendations")));
  window.addEventListener("melodarr-lidarr-settings-changed", () => {
    lidarrExternalUrlVersion += 1;
    lidarrExternalUrl = undefined;
    lidarrExternalUrlRequest = undefined;
  });
  window.addEventListener("melodarr-signed-out", () => {
    recommendationRequestVersion += 1;
    searchRequestVersion += 1;
    recommendationAbort?.abort();
    searchAbort?.abort();
    clearTimeout(recommendationPoll);
    clearTimeout(searchDebounce);
    stopArtistRevalidation();
    stopDetailAvailability();
    currentDetail = null;
    currentDetailData = undefined;
    detailHistory.length = 0;
    requestedArtist = undefined;
    $("#recommendation-results").replaceChildren();
    $("#results").replaceChildren();
    $("#results").removeAttribute("aria-busy");
    $("#results").setAttribute("aria-label", "Search results");
  });

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
    currentDetailData = undefined;
    stopDetailAvailability();
    detailHistory.length = 0;
    searchRequestVersion += 1;
    clearTimeout(searchDebounce);
    searchAbort?.abort();
    requestedArtist = undefined;
    $("#search-form").classList.remove("searching");
    $("#search-form").reset();
    activeSearchType = "artist";
    applySearchMode(activeSearchType);
    $("#search-message").textContent = "";
    $("#results").replaceChildren();
    $("#results").removeAttribute("aria-busy");
    $("#results").setAttribute("aria-label", "Search results");
    // Recommendation cards remain current through their own refresh events.
    // Keeping them mounted avoids refetching every thumbnail on navigation.
  });

  function showDetailFromLocation() {
    const match = window.location.pathname.match(/^\/(artists|albums|releases|anime|series)\/([^/]+)$/);
    if (!match) return false;
    const routes: Record<string, DetailKind> = {
      artists: "artist",
      albums: "release-group",
      releases: "release",
      anime: "anime",
      series: "series",
    };
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
          ["artist", "release-group", "release", "anime", "series"].includes(entry?.kind)
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
    currentDetailData = undefined;
    stopDetailAvailability();
    stopAnimeResolution();
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
      if (
        currentDetail?.kind === "artist"
        && currentDetail.id === requestedArtist.id
        && currentDetailData
      ) {
        startDetailAvailability("artist", currentDetailData, 0);
      }
    } catch (error) {
      $("#request-message").textContent = error.message;
    }
  });
})();
