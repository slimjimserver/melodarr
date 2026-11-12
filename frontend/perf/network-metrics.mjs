import { performance } from "node:perf_hooks";
import { setTimeout as delay } from "node:timers/promises";

// Decimal bits/s -> bytes/s, as required by CDP. These are chosen benchmark
// profiles, not a claim about the speed of every LTE connection.
export const NETWORK_PROFILES = [
  {
    name: "baseline",
    downloadBitsPerSecond: null,
    uploadBitsPerSecond: null,
    latencyMs: 0,
  },
  {
    name: "normal-lte",
    downloadBitsPerSecond: 8_000_000,
    uploadBitsPerSecond: 2_000_000,
    latencyMs: 70,
  },
  {
    name: "poor-lte",
    downloadBitsPerSecond: 1_500_000,
    uploadBitsPerSecond: 500_000,
    latencyMs: 150,
  },
];

export function networkConditions(profile) {
  return {
    offline: false,
    latency: profile.latencyMs,
    downloadThroughput:
      profile.downloadBitsPerSecond === null
        ? -1
        : profile.downloadBitsPerSecond / 8,
    uploadThroughput:
      profile.uploadBitsPerSecond === null
        ? -1
        : profile.uploadBitsPerSecond / 8,
  };
}

export function isRecommendation(url) {
  return new URL(url).pathname.startsWith("/api/discover");
}

// Retain only known public query values on known Melodarr API paths. Unknown
// queries, fragments, credentials and token-bearing path segments are omitted.
export function publicUrl(raw) {
  const url = new URL(raw);
  const path = url.pathname.replace(
    /(\/(?:invite|invites|invitation|invitations|token|tokens)\/)[^/]+/gi,
    "$1[redacted]",
  );
  const safe = new URLSearchParams();
  const keys =
    url.pathname === "/api/search"
      ? ["q", "type", "musicbrainz"]
      : url.pathname.startsWith("/api/artwork/")
        ? ["size"]
        : [];
  for (const key of keys) {
    if (url.searchParams.has(key)) safe.set(key, url.searchParams.get(key));
  }
  return `${url.origin}${path}${safe.size ? `?${safe}` : ""}`;
}

function category(record) {
  if (
    record.path.startsWith("/api/artwork/") ||
    record.type === "Image" ||
    record.mimeType?.startsWith("image/")
  )
    return "artwork";
  if (record.path.startsWith("/api/") || record.mimeType?.includes("json"))
    return "api";
  if (["Script", "Stylesheet", "Font"].includes(record.type)) return "static";
  return "other";
}

export class NetworkMetrics {
  constructor(cdp) {
    this.records = [];
    this.current = new Map();
    this.stage = "startup";
    this.startedAt = performance.now();
    this.lastActivityAt = this.startedAt;
    this.handlers = [];
    this.instrumentationError = null;
    const listen = (event, handler) => {
      const guarded = (payload) => {
        try {
          handler(payload);
        } catch {
          this.instrumentationError = `Invalid CDP event: ${event}`;
        }
      };
      cdp.on(event, guarded);
      this.handlers.push([event, guarded]);
    };
    listen("Network.requestWillBeSent", (event) => {
      if (!/^https?:/.test(event.request.url)) return;
      const previous = this.current.get(event.requestId);
      if (previous && event.redirectResponse) {
        this.response(previous, event.redirectResponse);
        this.finish(
          previous,
          event.redirectResponse.encodedDataLength,
          "redirect",
        );
      }
      const record = {
        url: publicUrl(event.request.url),
        path: new URL(event.request.url).pathname,
        method: event.request.method,
        type: event.type,
        stage: this.stage,
        excluded: isRecommendation(event.request.url),
        startedMs: performance.now() - this.startedAt,
        encodedChunks: 0,
        encodedDataLength: null,
        cache: false,
        state: "pending",
      };
      this.records.push(record);
      this.current.set(event.requestId, record);
      this.lastActivityAt = performance.now();
    });
    listen("Network.responseReceived", (event) => {
      const record = this.current.get(event.requestId);
      if (record) this.response(record, event.response);
    });
    listen("Network.requestServedFromCache", (event) => {
      const record = this.current.get(event.requestId);
      if (record) record.cache = true;
    });
    listen("Network.dataReceived", (event) => {
      const record = this.current.get(event.requestId);
      if (record) record.encodedChunks += event.encodedDataLength;
      this.lastActivityAt = performance.now();
    });
    listen("Network.loadingFinished", (event) => {
      const record = this.current.get(event.requestId);
      if (record) this.finish(record, event.encodedDataLength, "finished");
    });
    listen("Network.loadingFailed", (event) => {
      const record = this.current.get(event.requestId);
      if (record)
        this.finish(record, null, event.canceled ? "canceled" : "failed");
    });
  }

  response(record, response) {
    record.status = response.status;
    record.mimeType = response.mimeType;
    record.cache ||= Boolean(
      response.fromDiskCache || response.fromPrefetchCache,
    );
    if (response.fromServiceWorker)
      this.instrumentationError =
        "Unexpected service-worker response; page CDP cannot account for its network traffic.";
  }

  finish(record, bytes, state) {
    if (bytes !== null && (!Number.isFinite(bytes) || bytes < 0))
      throw new Error("Missing encoded transfer size");
    record.encodedDataLength = bytes;
    record.state = state;
    record.finishedMs = performance.now() - this.startedAt;
    this.lastActivityAt = performance.now();
  }

  snapshot() {
    if (this.instrumentationError) throw new Error(this.instrumentationError);
    const inventory = this.records
      .filter((record) => !record.excluded)
      .map((record) => ({
        url: record.url,
        method: record.method,
        stage: record.stage,
        category: category(record),
        type: record.type,
        status: record.status ?? null,
        state: record.state,
        cache: record.cache,
        startedMs: record.startedMs,
        finishedMs: record.finishedMs ?? null,
        cdpEncodedDataLength: record.encodedDataLength,
        transferredBytes: record.cache
          ? 0
          : (record.encodedDataLength ?? record.encodedChunks),
        byteSource: record.cache
          ? "browser-cache"
          : record.encodedDataLength !== null
            ? record.state === "redirect"
              ? "redirectResponse.encodedDataLength"
              : "loadingFinished.encodedDataLength"
            : "dataReceived.encodedDataLength (partial lower bound)",
      }));
    const totals = (rows) => ({
      requests: rows.length,
      transferredBytes: rows.reduce(
        (sum, row) => sum + row.transferredBytes,
        0,
      ),
      apiBytes: rows
        .filter((row) => row.category === "api")
        .reduce((sum, row) => sum + row.transferredBytes, 0),
      artworkBytes: rows
        .filter((row) => row.category === "artwork")
        .reduce((sum, row) => sum + row.transferredBytes, 0),
      staticBytes: rows
        .filter((row) => row.category === "static")
        .reduce((sum, row) => sum + row.transferredBytes, 0),
      otherBytes: rows
        .filter((row) => row.category === "other")
        .reduce((sum, row) => sum + row.transferredBytes, 0),
      artworkRequests: rows.filter((row) => row.category === "artwork").length,
      cachedRequests: rows.filter((row) => row.cache).length,
      pendingRequests: rows.filter((row) => row.state === "pending").length,
      failedRequests: rows.filter(
        (row) => row.state === "failed" || row.state === "canceled",
      ).length,
    });
    return {
      ...totals(inventory),
      excludedRecommendationRequests: this.records.filter(
        (record) => record.excluded,
      ).length,
      stages: Object.fromEntries(
        [...new Set(inventory.map((row) => row.stage))].map((stage) => [
          stage,
          totals(inventory.filter((row) => row.stage === stage)),
        ]),
      ),
      inventory: inventory.sort(
        (a, b) => b.transferredBytes - a.transferredBytes,
      ),
    };
  }

  async waitForQuiet(idleMs, timeoutMs) {
    const start = performance.now();
    while (performance.now() - start < timeoutMs) {
      if (
        !this.records.some(
          (record) => !record.excluded && record.state === "pending",
        ) &&
        performance.now() - this.lastActivityAt >= idleMs
      ) {
        return {
          reached: true,
          observedAfterUsableMs: performance.now() - start,
        };
      }
      // Poll actual network state only; this delay never models network speed
      // and is outside all interactive timing milestones.
      await delay(Math.min(50, timeoutMs - (performance.now() - start)));
    }
    return { reached: false, observedAfterUsableMs: performance.now() - start };
  }

  detach(cdp) {
    for (const [event, handler] of this.handlers) cdp.off(event, handler);
  }
}

export function requestIsAllowed(config, request, armed) {
  if (!config.allowRequest || !armed || request.method !== "POST") return false;
  const url = new URL(request.url);
  if (
    url.origin !== new URL(config.url).origin ||
    url.pathname !== "/api/request/release-group"
  )
    return false;
  try {
    return JSON.parse(request.postData).mbid === config.releaseGroupId;
  } catch {
    return false;
  }
}
