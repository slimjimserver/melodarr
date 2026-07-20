(() => {
  type DetailKind = "artist" | "release-group" | "release";
  type DetailReference = { kind: DetailKind; id: string };
  type ArtworkItem = { image: HTMLImageElement; source: string; fallback: HTMLElement };
  type ArtworkJob = { guard: ReturnType<typeof setTimeout> };
  type DetailRequest = { prefetch: boolean; settled: boolean; promise: Promise<JsonObject> | null };

  const $ = <T extends Element = AppElement>(selector: string): T => {
    const element = document.querySelector<T>(selector);
    if (!element) throw new Error(`Required element not found: ${selector}`);
    return element;
  };
  let currentDetail: DetailReference | null = null;
  const detailHistory: DetailReference[] = [];
  let requestedArtist: JsonObject | undefined;
  let lidarrExternalUrl: string | undefined;
  let plexArtists: Map<string, JsonObject> | undefined;
  let recommendationPoll: ReturnType<typeof setTimeout> | undefined;
  let searchRequestVersion = 0;
  const detailRequests = new Map<string, DetailRequest>();
  const detailUpgrades = new Map<string, Promise<JsonObject>>();
  const artworkQueue: ArtworkItem[] = [];
  const deferredArtwork = new WeakMap<Element, Omit<ArtworkItem, "image">>();
  const activeArtworkLoads = new Map<HTMLImageElement, ArtworkJob>();
  const maxArtworkRequests = 3;

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

  async function getJson(url: string, timeoutMilliseconds = 30_000): Promise<JsonObject> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMilliseconds);

    try {
      const response = await fetch(url, { signal: controller.signal });
    const body = await response.json() as JsonObject;
      if (!response.ok) throw new Error(body.error || "MusicBrainz request failed.");
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  function normalizedArtistName(name: string) {
    return (name || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  async function getLidarrExternalUrl() {
    if (lidarrExternalUrl !== undefined) return lidarrExternalUrl;
    const settings = await getJson("/api/settings");
    lidarrExternalUrl = settings.lidarr.externalUrl || "";
    return lidarrExternalUrl;
  }

  async function getPlexArtists() {
    if (plexArtists !== undefined) return plexArtists;
    try {
      const library = await getJson("/api/library");
      plexArtists = new Map(library.artists.map((artist: JsonObject) => [normalizedArtistName(artist.name), artist]));
    } catch {
      plexArtists = new Map();
    }
    return plexArtists;
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
    document.querySelectorAll(".view, .nav-link").forEach((element) => element.classList.remove("active"));
    $(`#${id}`).classList.add("active");
    if (id === "discover") $("[data-view=discover]").classList.add("active");
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

  function addPlexAvailability(element: HTMLElement, label = "Available in Plex") {
    const badge = document.createElement("span");
    badge.className = "plex-availability";
    badge.textContent = label;
    element.append(badge);
  }

  function createPlexAvailabilityPanel(releases: JsonObject[], message = "") {
    const panel = document.createElement("div");
    panel.className = "plex-availability-panel";
    addPlexAvailability(panel);
    const copy = document.createElement("span");
    copy.textContent = message || (releases.length === 1
      ? "One edition is in your selected Plex libraries."
      : `${releases.length} editions are in your selected Plex libraries.`);
    panel.append(copy);
    const target = releases.find((release) => release.url)?.url;
    if (target) {
      const link = document.createElement("a");
      link.href = target;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "Open in Plex";
      panel.append(link);
    }
    return panel;
  }

  function detailPath(kind: DetailKind, id: string) {
    const route: Record<DetailKind, string> = { artist: "artists", "release-group": "albums", release: "releases" };
    return `/${route[kind]}/${encodeURIComponent(id)}`;
  }

  function loadDetail(kind: DetailKind, id: string, prefetch = false): Promise<JsonObject> {
    const key = `${kind}:${id}`;
    const existing = detailRequests.get(key);
    if (existing && (prefetch || !existing.prefetch || existing.settled)) {
      return existing.promise!;
    }

    const entry: DetailRequest = { prefetch, settled: false, promise: null };
    const query = prefetch ? "?prefetch=1" : "";
    const timeout = prefetch ? 30_000 : kind === "artist" ? 120_000 : 60_000;
    entry.promise = getJson(
      `/api/music/${kind}/${encodeURIComponent(id)}${query}`,
      timeout,
    )
      .then((data) => {
        entry.settled = true;
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
    let upgrade = detailUpgrades.get(key);
    if (!upgrade) {
      upgrade = getJson(
        `/api/music/${kind}/${encodeURIComponent(id)}?complete=1`,
        120_000,
      );
      detailUpgrades.set(key, upgrade);
    }
    upgrade
      .then((data) => {
        detailRequests.set(key, {
          prefetch: false,
          settled: true,
          promise: Promise.resolve(data),
        });
        if (currentDetail?.kind !== kind || currentDetail.id !== id) return;
        renderDetail(kind, data);
        $("#detail-message").textContent = kind === "artist"
          ? "Complete discography loaded from MusicBrainz."
          : "Complete release information loaded from MusicBrainz.";
      })
      .catch((error) => {
        detailUpgrades.delete(key);
        if (currentDetail?.kind !== kind || currentDetail.id !== id) return;
        $("#detail-message").textContent = error.name === "AbortError"
          ? "The Lidarr metadata is shown. MusicBrainz is still taking too long to complete this page."
          : `The Lidarr metadata is shown. MusicBrainz enrichment failed: ${error.message}`;
      });
  }

  function showDetail(kind: DetailKind, id: string, addToHistory = true, updateHistory = true) {
    if (addToHistory && currentDetail) detailHistory.push(currentDetail);
    currentDetail = { kind, id };
    if (updateHistory) window.history.pushState({ kind, id }, "", detailPath(kind, id));
    const previous = detailHistory.at(-1);
    $("#back-to-search").textContent = previous
      ? `← Back to ${previous.kind === "artist" ? "artist" : previous.kind === "release-group" ? "album" : "release"}`
      : "← Back to search";
    showView("detail");
    $("#detail-results").replaceChildren();
    $("#detail-title").textContent = "";
    $("#detail-eyebrow").textContent = "";
    $("#detail-subtitle").textContent = "";
    $("#detail-cover").hidden = true;
    $("#detail-cover-image").removeAttribute("src");
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

  function createServiceIconLink(url: string, icon: string, label: string, className = "") {
    const link = document.createElement("a");
    link.className = className;
    link.href = url;
    link.title = label;
    link.setAttribute("aria-label", label);
    link.target = "_blank";
    link.rel = "noreferrer";
    link.addEventListener("click", (event) => event.stopPropagation());
    const image = document.createElement("img");
    image.src = icon;
    image.alt = "";
    link.append(image);
    return link;
  }

  function addExternalLinks(container: Element, kind: DetailKind, id: string, spotify?: string, plexUrl = "") {
    const links = document.createElement("div");
    links.className = "external-icons";
    const destinations = [
      ["/icons/musicbrainz.svg", `https://musicbrainz.org/${kind}/${id}`, "Open on MusicBrainz"],
      ...(spotify ? [["/icons/spotify.svg", spotify, "Open on Spotify"]] : []),
      ...(plexUrl ? [["/icons/plex.svg", plexUrl, "Open in Plex"]] : []),
    ];

    destinations.forEach(([icon, url, label]) => links.append(
      createServiceIconLink(url, icon, label),
    ));
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
    const plexUrl = kind === "artist" && data.availableInPlex ? data.plexUrl : "";
    addExternalLinks(meta, kind, data.id, data.spotify, plexUrl);
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
      $("#detail-message").textContent = result.message;
      button.textContent = result.alreadyExists
        ? "Available"
        : (result.pending ? "Queued" : "Requested");
    } catch (error) {
      $("#detail-message").textContent = error.message;
      button.textContent = "Request release group";
      button.disabled = false;
    }
  }

  function createSearchArtistCard(artist: JsonObject, description: string) {
    const card = createCard(artist.name, description, () => showDetail("artist", artist.id), artist.coverArt, "artist", artist.id);
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
    const card = createCard(artist.name, description, () => showDetail("artist", artist.id), artist.coverArt, "artist", artist.id);
    const services = document.createElement("div");
    services.className = "card-service-icons";
    services.append(createServiceIconLink(
      plexArtist.url,
      "/icons/plex.svg",
      "Open in Plex",
      "service-icon-link",
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
      image.fetchPriority = "low";
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

  function renderDetail(kind: DetailKind, data: JsonObject) {
    const results = $("#detail-results");
    results.replaceChildren();
    $("#detail-message").textContent = "";

    if (kind === "artist") {
      $("#detail-eyebrow").textContent = "ARTIST DISCOGRAPHY";
      $("#detail-title").textContent = data.name;
      const cover = $("#detail-cover");
      const coverImage = $("#detail-cover-image");
      if (data.coverArtLarge) {
        cover.hidden = false;
        coverImage.fetchPriority = "high";
        coverImage.src = data.coverArtLarge;
        coverImage.alt = `Artist image for ${data.name}`;
        coverImage.onerror = () => { cover.hidden = true; };
      }
      $("#detail-subtitle").textContent = [data.country, data.disambiguation].filter(Boolean).join(" · ");
      const meta = createMeta("artist", data);
      const facts = [data.type, data.gender, data.area, data.lifeSpan?.begin].filter(Boolean).join(" · ");
      if (facts) meta.append(document.createElement("br"), `Artist information: ${facts}`);
      results.append(meta);
      const requestButton = document.createElement("button");
      requestButton.className = "request-artist";
      requestButton.textContent = "Add artist to Lidarr";
      requestButton.addEventListener("click", () => openRequestDialog(data));
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
          detailRequests.set(`artist:${data.id}`, {
            prefetch: false,
            settled: true,
            promise: Promise.resolve(refreshed),
          });
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

      const layout = document.createElement("div");
      layout.className = "discography-layout";
      const index = document.createElement("nav");
      index.className = "discography-nav";
      const content = document.createElement("div");
      content.className = "discography-content";

      (Object.entries(data.sections || {}) as Array<[string, JsonObject[]]>).forEach(([name, groups], position) => {
        const section = document.createElement("details");
        const sectionId = `release-type-${position}`;
        section.id = sectionId;
        section.className = "discography-section";
        section.open = ["Album", "EP", "Single"].includes(name);
        const summary = document.createElement("summary");
        summary.textContent = `${name} (${groups.length})`;
        section.append(summary);
        groups.forEach((group) => {
          const card = createCard(
            group.title,
            [group.date, group.disambiguation].filter(Boolean).join(" · "),
            () => showDetail("release-group", group.id),
            group.coverArt,
            "release-group",
            group.id,
          );
          if (group.availableInPlex) addPlexAvailability(card);
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
          section.append(card);
        });
        content.append(section);

        const link = document.createElement("a");
        link.href = `#${sectionId}`;
        link.textContent = name;
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
      layout.append(index, content);
      results.append(layout);
      return;
    }

    if (kind === "release-group") {
      $("#detail-eyebrow").textContent = "ALBUM RELEASES";
      $("#detail-title").textContent = data.title;
      const cover = $("#detail-cover");
      const coverImage = $("#detail-cover-image");
      if (data.coverArtLarge) {
        cover.hidden = false;
        coverImage.fetchPriority = "high";
        coverImage.src = data.coverArtLarge;
        coverImage.alt = `Cover art for ${data.title}`;
        coverImage.onerror = () => { cover.hidden = true; };
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
      if (data.availableInPlex) {
        results.append(createPlexAvailabilityPanel(data.plexReleases || []));
      }
      const requestButton = document.createElement("button");
      requestButton.className = "request-artist";
      requestButton.textContent = "Request release group";
      requestButton.addEventListener("click", () => requestReleaseGroup({ id: data.id, button: requestButton }));
      results.append(requestButton);
      data.releases.forEach((release: JsonObject) => {
        const card = createCard(
          release.title,
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

  $("#search-type").addEventListener("change", (event) => {
    $("#search-input").placeholder = (event.target as HTMLSelectElement).value === "artist" ? "Search artists…" : "Search albums…";
  });

  $("#search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const requestVersion = ++searchRequestVersion;
    const query = $("#search-input").value.trim();
    const type = $("#search-type").value;
    const results = $("#results");
    results.replaceChildren();

    if (query.length < 2) {
      $("#search-message").textContent = "Enter at least two characters.";
      return;
    }

    $("#search-message").textContent = "Searching MusicBrainz…";
    try {
      const [data, library] = await Promise.all([getJson(`/api/search?q=${encodeURIComponent(query)}&type=${type}`), getPlexArtists()]);
      if (requestVersion !== searchRequestVersion) return;
      $("#search-message").textContent = data.results.length
        ? `${data.results.length} ${type === "artist" ? "artists" : "albums"} found`
        : "No results found.";
      data.results.forEach((result: JsonObject) => {
        const description = type === "artist"
          ? [result.type, result.country, result.disambiguation].filter(Boolean).join(" · ")
          : [result.artist, result.type, result.date, result.disambiguation].filter(Boolean).join(" · ");
        const plexArtist = library.get(normalizedArtistName(result.name));
        results.append(type === "artist"
          ? (plexArtist ? createPlexArtistCard(result, description, plexArtist) : createSearchArtistCard(result, description))
          : createCard(result.name, description, () => showDetail("release-group", result.id)));
      });
    } catch (error) {
      if (requestVersion !== searchRequestVersion) return;
      $("#search-message").textContent = error.message;
    }
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
    results.replaceChildren();
    message.textContent = "Loading cached recommendations…";
    try {
      const data = await getJson("/api/discover");
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
      window.history.replaceState({ kind: previous.kind, id: previous.id }, "", detailPath(previous.kind, previous.id));
    } else {
      currentDetail = null;
      showView("discover");
      window.history.replaceState({ view: "discover" }, "", "/");
    }
  });

  window.addEventListener("melodarr-home", () => {
    currentDetail = null;
    detailHistory.length = 0;
    searchRequestVersion += 1;
    requestedArtist = undefined;
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
    showDetail(kind, decodeURIComponent(match[2]), false, false);
    return true;
  }

  window.addEventListener("popstate", showDetailFromLocation);
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
      $("#detail-message").textContent = result.message;
    } catch (error) {
      $("#request-message").textContent = error.message;
    }
  });
})();
