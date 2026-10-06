import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { configuration, markdown } from "./artist-request-benchmark.mjs";
import {
  NETWORK_PROFILES,
  NetworkMetrics,
  networkConditions,
  publicUrl,
  requestIsAllowed,
} from "./network-metrics.mjs";

test("profile units and unrestricted baseline match CDP", () => {
  assert.deepEqual(NETWORK_PROFILES.map(networkConditions), [
    {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    },
    {
      offline: false,
      latency: 70,
      downloadThroughput: 1_000_000,
      uploadThroughput: 250_000,
    },
    {
      offline: false,
      latency: 150,
      downloadThroughput: 187_500,
      uploadThroughput: 62_500,
    },
  ]);
});

test("encoded transfers, partial failures, redirects and cache hits are counted once by initiating stage", () => {
  const cdp = new EventEmitter();
  const metrics = new NetworkMetrics(cdp);
  const request = (id, url, type) =>
    cdp.emit("Network.requestWillBeSent", {
      requestId: id,
      type,
      request: { url: `https://melodarr.test${url}`, method: "GET" },
    });
  request("api", "/api/search?q=Artist&api_key=secret", "Fetch");
  cdp.emit("Network.dataReceived", { requestId: "api", encodedDataLength: 30 });
  metrics.stage = "discography";
  cdp.emit("Network.loadingFinished", {
    requestId: "api",
    encodedDataLength: 100,
  });
  request("art", "/api/artwork/artist/id?size=card&token=secret", "Image");
  cdp.emit("Network.loadingFinished", {
    requestId: "art",
    encodedDataLength: 200,
  });
  request("cached", "/api/artwork/release-group/id", "Image");
  cdp.emit("Network.requestServedFromCache", { requestId: "cached" });
  cdp.emit("Network.loadingFinished", {
    requestId: "cached",
    encodedDataLength: 9999,
  });
  request("script", "/static/app.js", "Script");
  cdp.emit("Network.dataReceived", {
    requestId: "script",
    encodedDataLength: 20,
  });
  cdp.emit("Network.loadingFailed", { requestId: "script", canceled: true });
  request("pending", "/long-request", "Fetch");
  cdp.emit("Network.dataReceived", {
    requestId: "pending",
    encodedDataLength: 10,
  });
  request("redirect", "/redirect", "Document");
  cdp.emit("Network.requestWillBeSent", {
    requestId: "redirect",
    type: "Document",
    request: { url: "https://melodarr.test/", method: "GET" },
    redirectResponse: {
      status: 302,
      encodedDataLength: 50,
      mimeType: "text/html",
    },
  });
  cdp.emit("Network.loadingFinished", {
    requestId: "redirect",
    encodedDataLength: 70,
  });
  request("discovery", "/api/discover?key=secret", "Fetch");
  cdp.emit("Network.loadingFailed", { requestId: "discovery" });
  const result = metrics.snapshot();
  assert.equal(result.requests, 7);
  assert.equal(result.transferredBytes, 450);
  assert.equal(result.apiBytes, 100);
  assert.equal(result.artworkBytes, 200);
  assert.equal(result.staticBytes, 20);
  assert.equal(result.otherBytes, 130);
  assert.equal(result.artworkRequests, 2);
  assert.equal(result.cachedRequests, 1);
  assert.equal(result.pendingRequests, 1);
  assert.equal(result.failedRequests, 1);
  assert.equal(result.stages.startup.apiBytes, 100);
  assert.equal(result.excludedRecommendationRequests, 1);
  assert.equal(JSON.stringify(result).includes("secret"), false);
  assert.deepEqual(
    result.inventory.map((row) => row.transferredBytes),
    [200, 100, 70, 50, 20, 10, 0],
  );
  metrics.detach(cdp);
  assert.equal(cdp.listenerCount("Network.loadingFinished"), 0);
});

test("failed instrumentation is functional failure, not a performance budget", () => {
  const cdp = new EventEmitter();
  const metrics = new NetworkMetrics(cdp);
  cdp.emit("Network.requestWillBeSent", {
    requestId: "x",
    request: { url: "https://melodarr.test/", method: "GET" },
  });
  cdp.emit("Network.loadingFinished", { requestId: "x" });
  assert.throws(() => metrics.snapshot(), /Invalid CDP event/);
});

test("request permission requires exact flag, armed stage, origin, endpoint and MBID", () => {
  const config = {
    allowRequest: true,
    url: "https://melodarr.test/",
    releaseGroupId: "chosen-id",
  };
  const request = {
    method: "POST",
    url: "https://melodarr.test/api/request/release-group",
    postData: '{"mbid":"chosen-id"}',
  };
  assert.equal(requestIsAllowed(config, request, true), true);
  assert.equal(
    requestIsAllowed({ ...config, allowRequest: false }, request, true),
    false,
  );
  assert.equal(requestIsAllowed(config, request, false), false);
  for (const change of [
    { method: "GET" },
    { url: "https://other.test/api/request/release-group" },
    { url: "https://melodarr.test/api/request/artist" },
    { postData: '{"mbid":"arbitrary-id"}' },
    { postData: "bad JSON" },
  ]) {
    assert.equal(
      requestIsAllowed(config, { ...request, ...change }, true),
      false,
    );
  }
});

test("URL inventory removes credentials, unknown queries, invite tokens and fragments", () => {
  assert.equal(
    publicUrl(
      "https://user:password@melodarr.test/api/search?q=Artist&type=artist&Plex-Token=secret#invite",
    ),
    "https://melodarr.test/api/search?q=Artist&type=artist",
  );
  assert.equal(
    publicUrl("https://melodarr.test/invites/secret?foo=secret"),
    "https://melodarr.test/invites/[redacted]",
  );
  assert.equal(
    publicUrl("https://artwork.test/image?X-Amz-Signature=secret&size=card"),
    "https://artwork.test/image",
  );
});

test("configuration never infers request permission from a URL or fixture mode", () => {
  assert.equal(configuration({}, true).allowRequest, false);
  assert.equal(
    configuration({ BENCHMARK_ALLOW_REQUEST: "true" }, true).allowRequest,
    false,
  );
  assert.equal(
    configuration({ BENCHMARK_ALLOW_REQUEST: "1" }, true).allowRequest,
    true,
  );
  assert.throws(() => configuration({}), /MELODARR_BENCHMARK_URL/);
  assert.throws(
    () => configuration({ BENCHMARK_PROFILES: "unknown" }, true),
    /Unknown/,
  );
});

test("skipped requests never look like an accepted complete journey", () => {
  const output = markdown({
    generatedAt: "fixture",
    fixture: true,
    runs: [
      {
        profile: "poor-lte",
        cache: "cold",
        repetition: 1,
        requestStatus: "skipped",
        requestMs: null,
        totalJourneyMs: null,
        timeToRequestControlMs: 1234.5,
      },
    ],
  });
  assert.match(output, /skipped/);
  assert.match(output, /1234.5 ms/);
  assert.match(output, /Journey to acceptance/);
});
