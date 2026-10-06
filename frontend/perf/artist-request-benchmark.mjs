import { chromium, expect } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createFixtureServer } from "../tests/fixture-server.mjs";
import {
  NETWORK_PROFILES,
  NetworkMetrics,
  networkConditions,
  publicUrl,
  requestIsAllowed,
} from "./network-metrics.mjs";

export function configuration(env = process.env, fixture = false) {
  const required = (name) => {
    if (!env[name]?.trim())
      throw new Error(`Set ${name} before running the benchmark.`);
    return env[name].trim();
  };
  const integer = (name, fallback, minimum = 1) => {
    const value = Number(env[name] ?? fallback);
    if (!Number.isSafeInteger(value) || value < minimum)
      throw new Error(`${name} must be an integer >= ${minimum}.`);
    return value;
  };
  const url = new URL(
    fixture ? "http://127.0.0.1/" : required("MELODARR_BENCHMARK_URL"),
  );
  if (
    !/^https?:$/.test(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !["/", "/discover"].includes(url.pathname)
  ) {
    throw new Error(
      "MELODARR_BENCHMARK_URL must be an HTTP(S) home URL without credentials, queries or fragments.",
    );
  }
  const profiles = (
    env.BENCHMARK_PROFILES ||
    NETWORK_PROFILES.map((profile) => profile.name).join(",")
  ).split(",");
  if (
    profiles.some(
      (name) => !NETWORK_PROFILES.some((profile) => profile.name === name),
    )
  )
    throw new Error("Unknown BENCHMARK_PROFILES entry.");
  const config = {
    url: url.href,
    fixture,
    artistQuery: fixture
      ? "Fixture Artist"
      : required("BENCHMARK_ARTIST_QUERY"),
    artistId: fixture ? "fixture-artist" : required("BENCHMARK_ARTIST_MBID"),
    releaseGroupId: fixture
      ? "fixture-album"
      : required("BENCHMARK_RELEASE_GROUP_MBID"),
    storageState: fixture ? null : env.MELODARR_BENCHMARK_STORAGE_STATE || null,
    allowRequest: env.BENCHMARK_ALLOW_REQUEST === "1",
    openReleaseDetail: env.BENCHMARK_OPEN_RELEASE_DETAIL === "1",
    releaseSection: env.BENCHMARK_RELEASE_SECTION || null,
    releaseFilter: env.BENCHMARK_RELEASE_FILTER || null,
    profiles: NETWORK_PROFILES.filter((profile) =>
      profiles.includes(profile.name),
    ),
    repetitions: integer("BENCHMARK_REPETITIONS", 1),
    timeoutMs: integer("BENCHMARK_TIMEOUT_MS", 60_000),
    quietTimeoutMs: integer("BENCHMARK_QUIET_TIMEOUT_MS", 5_000),
    quietIdleMs: integer("BENCHMARK_QUIET_IDLE_MS", 500),
    outputDirectory: resolve(
      env.BENCHMARK_OUTPUT_DIR || "../benchmark-results",
    ),
  };
  if (config.artistQuery.length < 2)
    throw new Error(
      "BENCHMARK_ARTIST_QUERY must have at least two characters.",
    );
  for (const id of [config.artistId, config.releaseGroupId]) {
    if (!/^[a-zA-Z0-9-]+$/.test(id))
      throw new Error(
        "Benchmark IDs must contain only letters, digits and hyphens.",
      );
  }
  return config;
}

export async function installMilestones(context, config) {
  await context.addInitScript(
    ({ artistPath, releasePath, releaseGroupId }) => {
      window.__benchmarkMilestones = {};
      window.addEventListener("melodarr-authenticated", () => {
        window.__benchmarkDiscoveryReady = true;
      });
      const mark = (name) => {
        window.__benchmarkMilestones[name] ??= performance.now();
      };
      document.addEventListener(
        "input",
        (event) => {
          if (event.target.id === "search-input") mark("searchStart");
        },
        true,
      );
      document.addEventListener(
        "click",
        (event) => {
          const link = event.target.closest("a");
          if (link?.getAttribute("href") === artistPath) mark("artistClick");
          if (link?.getAttribute("href") === releasePath)
            mark("releaseGroupClick");
          const button = event.target.closest("button");
          if (button?.id === "search-submit") mark("searchSubmit");
          if (
            button &&
            ((button.closest("[data-release-group-id]")?.dataset
              .releaseGroupId === releaseGroupId &&
              button.matches(".release-group-request")) ||
              (location.pathname === releasePath &&
                button.matches(".detail-actions .detail-availability-action")))
          )
            mark("requestClick");
        },
        true,
      );
    },
    {
      artistPath: `/artists/${config.artistId}`,
      releasePath: `/albums/${config.releaseGroupId}`,
      releaseGroupId: config.releaseGroupId,
    },
  );
}

async function markUsable(page, name) {
  await page.evaluate((name) => {
    window.__benchmarkMilestones[name] = performance.now();
  }, name);
}

async function usable(locator, assert) {
  await assert(locator).toBeVisible();
  await assert(locator).toBeEnabled();
  // Playwright's full actionability check includes overlays and scrolling to
  // this one intended control. It does not click or load below-fold images.
  await locator.tap({ trial: true });
}

export async function runJourney(
  page,
  cdp,
  config,
  profile,
  cache,
  repetition,
) {
  const assert = expect.configure({ timeout: config.timeoutMs });
  const ready = (locator) => usable(locator, assert);
  const metrics = new NetworkMetrics(cdp);
  const result = {
    profile: profile.name,
    cache,
    repetition,
    status: "running",
  };
  let armed = false;
  let guardError = null;
  let phase = "instrumentation";
  let requestSubmitted = false;
  const guard = async (event) => {
    try {
      if (requestIsAllowed(config, event.request, armed)) {
        armed = false; // Only one explicitly armed, matching submission per run.
        requestSubmitted = true;
        await cdp.send("Fetch.continueRequest", { requestId: event.requestId });
      } else {
        guardError = "Browser blocked an unapproved acquisition request.";
        await cdp.send("Fetch.failRequest", {
          requestId: event.requestId,
          errorReason: "BlockedByClient",
        });
      }
    } catch {
      guardError = "Browser request safety instrumentation failed.";
    }
  };
  cdp.on("Fetch.requestPaused", guard);
  try {
    await cdp.send("Fetch.enable", {
      patterns: [{ urlPattern: "*://*/api/request*", requestStage: "Request" }],
    });
    await cdp.send(
      "Network.emulateNetworkConditions",
      networkConditions(profile),
    );
    phase = "startup";
    await page.goto(config.url, { waitUntil: "domcontentloaded" });
    await assert(page.locator("body")).toHaveClass(/authenticated/);
    // Home markup can be visible before the deferred discovery bundle has
    // bound the form. Its existing event fires after that bundle executes.
    await page.waitForFunction(() => window.__benchmarkDiscoveryReady === true);
    await ready(page.locator("#search-input"));
    await page.locator("#search-type").selectOption("artist");
    await markUsable(page, "searchReady");

    phase = "search";
    metrics.stage = phase;
    await page.locator("#search-input").fill(config.artistQuery);
    await page.locator("#search-submit").tap();
    await assert(page.locator("#results")).not.toHaveAttribute(
      "aria-busy",
      "true",
    );
    const artist = page.locator(
      `#results a[href="/artists/${config.artistId}"]`,
    );
    // Pagination is a normal UI action, and its wait belongs to search time.
    while (
      (await artist.count()) === 0 &&
      (await page.locator(".search-show-more").count())
    ) {
      await page.locator(".search-show-more").tap();
    }
    await ready(artist);
    await markUsable(page, "usableSearchResults");

    phase = "discography";
    metrics.stage = phase;
    await artist.tap();
    await assert(page).toHaveURL(new RegExp(`/artists/${config.artistId}$`));
    await assert(page.locator("#detail-title")).not.toHaveText("");
    await assert(page.locator(".artist-discography")).toBeVisible();
    await ready(
      page
        .locator('.artist-discography .artist-card a[href^="/albums/"]')
        .first(),
    );
    await markUsable(page, "usableDiscography");

    phase = "selection";
    metrics.stage = phase;
    if (config.releaseSection)
      await page
        .locator(".discography-nav")
        .getByRole("link", { name: config.releaseSection, exact: true })
        .tap();
    if (config.releaseFilter)
      await page.locator("#discography-search").fill(config.releaseFilter);
    const card = page.locator(
      `.artist-discography [data-release-group-id="${config.releaseGroupId}"]`,
    );
    await assert(card).toBeVisible();
    // Keep the locator stable when a successful request changes its label.
    const directRequest = card.locator("button.release-group-request");
    let requestControl;
    if (
      !config.openReleaseDetail &&
      (await directRequest.count()) &&
      (await directRequest.innerText()) === "Request"
    ) {
      requestControl = directRequest;
    } else {
      const release = card.locator(
        `a[href="/albums/${config.releaseGroupId}"]`,
      );
      await ready(release);
      phase = "release-group";
      metrics.stage = phase;
      await release.tap();
      await assert(page).toHaveURL(
        new RegExp(`/albums/${config.releaseGroupId}$`),
      );
      requestControl = page.locator(
        "#detail-results .detail-actions button.detail-availability-action",
      );
      await assert(requestControl).toHaveText("Request release group");
    }
    await ready(requestControl);
    await markUsable(page, "requestControlUsable");

    if (config.allowRequest) {
      phase = "request";
      metrics.stage = phase;
      armed = true;
      await requestControl.tap();
      // The existing UI changes the target control only after a successful
      // request response. Available also counts as an accepted idempotent call.
      await assert(requestControl).toHaveText(
        /^(Requested|Queued|Available|Downloading)$/,
      );
      if (!requestSubmitted)
        throw new Error("Request guard did not observe a submission.");
      await markUsable(page, "requestAccepted");
      result.requestStatus = "accepted";
    } else {
      result.requestStatus = "skipped";
    }
    if (guardError) throw new Error(guardError);
    const milestones = await page.evaluate(() => window.__benchmarkMilestones);
    const end = milestones.requestAccepted ?? milestones.requestControlUsable;
    const duration = (start, finish) => {
      if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start)
        throw new Error("Missing browser timing milestone.");
      return Math.round((finish - start) * 10) / 10;
    };
    result.milestonesMs = milestones;
    result.startupMs = duration(0, milestones.searchReady);
    result.searchMs = duration(
      milestones.searchStart,
      milestones.usableSearchResults,
    );
    result.artistDetailMs = duration(
      milestones.artistClick,
      milestones.usableDiscography,
    );
    result.selectionMs = duration(
      milestones.usableDiscography,
      milestones.releaseGroupClick ?? milestones.requestControlUsable,
    );
    result.releaseGroupMs =
      milestones.releaseGroupClick === undefined
        ? null
        : duration(
            milestones.releaseGroupClick,
            milestones.requestControlUsable,
          );
    result.requestMs = config.allowRequest
      ? duration(milestones.requestClick, milestones.requestAccepted)
      : null;
    result.totalJourneyMs = config.allowRequest
      ? duration(milestones.searchStart, end)
      : null;
    result.timeToRequestControlMs = duration(
      milestones.searchStart,
      milestones.requestControlUsable,
    );
    result.usableNetwork = metrics.snapshot();
    if (
      !result.usableNetwork.inventory.some(
        (row) =>
          row.type === "Document" &&
          row.cdpEncodedDataLength !== null &&
          (cache === "warm" || row.cdpEncodedDataLength > 0),
      )
    )
      throw new Error("CDP did not measure the document transfer.");
    metrics.stage = "after-usable";
    result.networkQuiet = await metrics.waitForQuiet(
      config.quietIdleMs,
      config.quietTimeoutMs,
    );
    result.observedNetwork = metrics.snapshot();
    if (guardError) throw new Error(guardError);
    result.status = "ok";
  } catch {
    // Playwright errors can embed signed URLs or authentication response data.
    // Save only a bounded, known diagnostic; never serialize raw errors/traces.
    result.status = "failed";
    result.failureStage = phase;
    result.error =
      guardError ||
      `The ${phase} stage failed: check authentication, target eligibility, UI availability and Chromium/CDP support. No performance budget is enforced.`;
    try {
      result.observedNetwork = metrics.snapshot();
    } catch {
      result.error = "CDP network instrumentation failed.";
    }
  } finally {
    armed = false;
    // Keep the acquisition guard active until the document is unloaded.
    await page.goto("about:blank").catch(() => {});
    await cdp.send("Fetch.disable").catch(() => {});
    cdp.off("Fetch.requestPaused", guard);
    metrics.detach(cdp);
  }
  return result;
}

export function bytes(value) {
  return value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(2)} MB`
    : `${(value / 1_000).toFixed(1)} KB`;
}

export function markdown(report) {
  const ms = (value) =>
    value === null || value === undefined ? "—" : `${value.toFixed(1)} ms`;
  const lines = [
    "# Melodarr artist/request browser benchmark",
    "",
    `Generated: ${report.generatedAt} · Chromium ${report.chromiumVersion} · ${report.fixture ? "isolated HTTP fixture (no acquisition)" : "configured Melodarr instance"}`,
    "",
    "Recommendations are blocked and excluded. Server caches are retained. Transfers below end at the final usable milestone; pending transfers are partial lower bounds.",
    "",
    "| Profile | Cache | Run | Startup | Search | Discography | Selection | Release detail | Request | Journey to acceptance | To request control | HTTP requests | Transfer | Pending |",
    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
  ];
  if (report.error) lines.push("", report.error, "");
  for (const run of report.runs) {
    const net = run.usableNetwork ?? run.observedNetwork;
    lines.push(
      `| ${run.profile} | ${run.cache} | ${run.repetition} | ${ms(run.startupMs)} | ${ms(run.searchMs)} | ${ms(run.artistDetailMs)} | ${ms(run.selectionMs)} | ${ms(run.releaseGroupMs)} | ${run.requestStatus === "skipped" ? "skipped" : ms(run.requestMs)} | ${ms(run.totalJourneyMs)} | ${ms(run.timeToRequestControlMs)} | ${net?.requests ?? "—"} | ${net ? bytes(net.transferredBytes) : "—"} | ${net?.pendingRequests ?? "—"} |`,
    );
  }
  lines.push(
    "",
    "## Transfers at usability / after bounded network observation",
    "",
    "| Profile / cache / run | Window | API/JSON | Artwork | Static JS/CSS/fonts | HTML/other | Artwork requests | Cache hits | Failed/canceled |",
    "|---|---|---:|---:|---:|---:|---:|---:|---:|",
  );
  for (const run of report.runs) {
    for (const [window, net] of [
      ["usable", run.usableNetwork],
      ["observed", run.observedNetwork],
    ]) {
      if (net)
        lines.push(
          `| ${run.profile} / ${run.cache} / ${run.repetition} | ${window} | ${bytes(net.apiBytes)} | ${bytes(net.artworkBytes)} | ${bytes(net.staticBytes)} | ${bytes(net.otherBytes)} | ${net.artworkRequests} | ${net.cachedRequests} | ${net.failedRequests} |`,
        );
    }
  }
  lines.push("");
  for (const run of report.runs) {
    if (run.networkQuiet)
      lines.push(
        `\n${run.profile} / ${run.cache} / ${run.repetition}: quiet ${run.networkQuiet.reached ? "reached" : "timed out"} after ${ms(run.networkQuiet.observedAfterUsableMs)} beyond usability. This is not full loading of every lazy image.\n`,
      );
    if (run.status === "failed")
      lines.push(`\nFailed at ${run.failureStage}: ${run.error}\n`);
  }
  lines.push("", "## Observed request inventory (largest transfers first)", "");
  for (const run of report.runs) {
    lines.push(
      `### ${run.profile} / ${run.cache} / ${run.repetition}`,
      "",
      "```text",
    );
    for (const row of run.observedNetwork?.inventory ?? [])
      lines.push(
        `${bytes(row.transferredBytes).padStart(10)}  ${row.method} ${row.url}  [${row.stage}; ${row.state}${row.cache ? "; cache" : ""}]`,
      );
    lines.push("```", "");
  }
  return `${lines.join("\n")}\n`;
}

export async function benchmark(config) {
  let fixtureServer;
  let browser;
  const report = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    fixture: config.fixture,
    target: publicUrl(config.url),
    content: {
      artistQuery: config.artistQuery,
      artistId: config.artistId,
      releaseGroupId: config.releaseGroupId,
    },
    allowRequest: config.allowRequest,
    serverCaches: "retained",
    profiles: config.profiles,
    viewport: { width: 390, height: 844, deviceScaleFactor: 2 },
    quietObservation: {
      idleMs: config.quietIdleMs,
      timeoutMs: config.quietTimeoutMs,
    },
    workflow: {
      openReleaseDetail: config.openReleaseDetail,
      releaseSection: config.releaseSection,
      releaseFilter: config.releaseFilter,
    },
    runs: [],
  };
  try {
    if (config.fixture) {
      fixtureServer = createFixtureServer({ benchmark: true });
      await new Promise((resolve, reject) => {
        fixtureServer.once("error", reject);
        fixtureServer.listen(0, "127.0.0.1", resolve);
      });
      config = {
        ...config,
        url: `http://127.0.0.1:${fixtureServer.address().port}/`,
      };
      report.target = publicUrl(config.url);
    }
    // Authentication cookies only: do not seed prior frontend/localStorage or
    // IndexedDB data into a supposedly cold browser.
    const storageState = config.storageState
      ? {
          cookies: JSON.parse(await readFile(config.storageState, "utf8"))
            .cookies,
          origins: [],
        }
      : undefined;
    browser = await chromium.launch();
    report.chromiumVersion = browser.version();
    for (const profile of config.profiles) {
      for (
        let repetition = 1;
        repetition <= config.repetitions;
        repetition += 1
      ) {
        const context = await browser.newContext({
          storageState,
          viewport: { width: 390, height: 844 },
          deviceScaleFactor: 2,
          isMobile: true,
          hasTouch: true,
          serviceWorkers: "block",
        });
        try {
          context.setDefaultTimeout(config.timeoutMs);
          context.setDefaultNavigationTimeout(config.timeoutMs);
          await installMilestones(context, config);
          const page = await context.newPage();
          const cdp = await context.newCDPSession(page);
          await cdp.send("Network.enable");
          await cdp.send("Network.setCacheDisabled", { cacheDisabled: false });
          await cdp.send("Network.clearBrowserCache");
          await cdp.send("Network.setBypassServiceWorker", { bypass: true });
          // CDP blocking preserves the HTTP cache; Playwright route() would
          // disable it and invalidate the warm-cache comparison.
          await cdp.send("Network.setBlockedURLs", {
            urls: ["*://*/api/discover*"],
          });
          for (const cache of ["cold", "warm"]) {
            const run = await runJourney(
              page,
              cdp,
              config,
              profile,
              cache,
              repetition,
            );
            report.runs.push(run);
            console.log(
              `${profile.name} ${cache} #${repetition}: ${run.status}; request ${run.requestStatus ?? "unreached"}; to control ${run.timeToRequestControlMs ?? "—"} ms`,
            );
            if (run.status !== "ok") break;
          }
        } finally {
          await context.close();
        }
        if (report.runs.at(-1)?.status !== "ok") break;
      }
      if (report.runs.at(-1)?.status !== "ok") break;
    }
    if (config.fixture) {
      report.fixtureRequests = (
        await (await fetch(`${config.url}__benchmark-state`)).json()
      ).requests;
      if (
        report.fixtureRequests !==
        report.runs.filter((run) => run.requestStatus === "accepted").length
      )
        throw new Error("Fixture request safety/count validation failed.");
    }
  } catch {
    report.error =
      "Benchmark setup/instrumentation failed. Check configuration, storage-state readability and the installed Playwright Chromium browser.";
  } finally {
    await browser?.close();
    if (fixtureServer)
      await new Promise((resolve) => fixtureServer.close(resolve));
  }
  await mkdir(config.outputDirectory, { recursive: true });
  const stem = resolve(
    config.outputDirectory,
    `${report.generatedAt.replace(/[:.]/g, "-")}-artist-request`,
  );
  await writeFile(`${stem}.json`, `${JSON.stringify(report, null, 2)}\n`);
  await writeFile(`${stem}.md`, markdown(report));
  console.log(`Results: ${stem}.json and ${stem}.md`);
  return report;
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const args = process.argv.slice(2);
  if (args.some((arg) => arg !== "--fixture")) {
    console.error(
      "Usage: npm run benchmark:lte [-- --fixture]. See perf/README.md for environment variables.",
    );
    process.exitCode = 1;
  } else {
    try {
      const report = await benchmark(
        configuration(process.env, args.includes("--fixture")),
      );
      if (report.error || report.runs.some((run) => run.status !== "ok"))
        process.exitCode = 1;
    } catch (error) {
      // Configuration errors contain only known names and validation messages.
      console.error(
        error instanceof TypeError
          ? "Invalid benchmark configuration."
          : error.message,
      );
      process.exitCode = 1;
    }
  }
}
