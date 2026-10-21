"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

global.location = { hash: "" };
global.document = { addEventListener() {} };
const { createPaperPoller, paperStatusPresentation } =
  require("../../app/static/arena-app.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function requests(statusRequest) {
  return [statusRequest].concat(Array.from({ length: 5 }, () => () => Promise.resolve({ ok: true })));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test("a never-settling status request expires stale and late responses are ignored", async () => {
  const status = { fresh: true, value: { state: "RUNNING" }, staleCount: 0, timedOut: false };
  let complete;
  const poller = createPaperPoller(requests(() => new Promise(() => {})), {
    onStatus(result) {
      status.fresh = result.status === "fulfilled";
      if (status.fresh) status.value = result.value;
      else status.staleCount += 1;
    },
    onStale() { status.fresh = false; status.staleCount += 1; },
    onComplete(_results, timedOut) { complete = timedOut; }
  }, 40, 150);

  try {
    assert.equal(poller.poll(), true);
    assert.equal(poller.poll(), false, "a second cycle must not overlap the pending one");
    await sleep(100);
    assert.equal(status.fresh, false, "a hung request must not leave RUNNING marked fresh");
    assert.equal(status.value.state, "RUNNING", "retain the last-known state");
    assert.equal(status.staleCount, 1);
    assert.equal(complete, true);
    assert.deepEqual(paperStatusPresentation(status.fresh, status.value), {
      stale: true,
      label: "PAPER STATUS STALE",
      lastKnown: "RUNNING",
      stopLabel: "STOP PAPER · LAST KNOWN RUNNING"
    });
  } finally {
    poller.stop();
  }
});

test("a rejected status request marks the last successful snapshot stale", async () => {
  let fresh = true;
  let complete;
  const poller = createPaperPoller(requests(() => Promise.reject(new Error("offline"))), {
    onStatus(result) { fresh = result.status === "fulfilled"; },
    onStale() { fresh = false; },
    onComplete(_results, timedOut) { complete = timedOut; }
  }, 100, 100);

  try {
    poller.poll();
    await sleep(10);
    assert.equal(complete, false);
    assert.equal(fresh, false);
  } finally {
    poller.stop();
  }
});

test("a delayed response from an expired poll cannot overwrite the newer status", async () => {
  const oldStatus = deferred();
  let calls = 0;
  let latest = null;
  const statusRequest = () => {
    calls += 1;
    return calls === 1 ? oldStatus.promise : Promise.resolve({ state: "STOPPED" });
  };
  const poller = createPaperPoller(requests(statusRequest), {
    onStatus(result) { if (result.status === "fulfilled") latest = result.value.state; },
    onStale() {},
    onComplete() {}
  }, 40, 200);

  try {
    poller.poll();
    await sleep(100); // first cycle times out and invalidates its pending promises
    assert.equal(poller.poll(), true);
    await sleep(20);
    assert.equal(latest, "STOPPED");
    oldStatus.resolve({ state: "RUNNING" });
    await sleep(10);
    assert.equal(latest, "STOPPED", "the late old response must have no mutation path");
  } finally {
    poller.stop();
  }
});

test("a previously RUNNING snapshot becomes explicitly stale and recovers on success", async () => {
  const snapshot = { fresh: false, value: { state: "RUNNING" } };
  let calls = 0;
  const poller = createPaperPoller(requests(() => {
    calls += 1;
    return Promise.resolve(calls === 1 ? { state: "RUNNING" } : { state: "PAUSED" });
  }), {
    onStatus(result) {
      snapshot.fresh = result.status === "fulfilled";
      if (snapshot.fresh) snapshot.value = result.value;
    },
    onStale() { snapshot.fresh = false; },
    onComplete() {}
  }, 150, 80);

  try {
    poller.poll();
    await sleep(20);
    assert.equal(snapshot.fresh, true);
    assert.deepEqual(paperStatusPresentation(snapshot.fresh, snapshot.value), {
      stale: false, label: null, lastKnown: null, stopLabel: null
    });

    await sleep(120);
    assert.equal(snapshot.fresh, false);
    assert.deepEqual(paperStatusPresentation(snapshot.fresh, snapshot.value), {
      stale: true,
      label: "PAPER STATUS STALE",
      lastKnown: "RUNNING",
      stopLabel: "STOP PAPER · LAST KNOWN RUNNING"
    });

    assert.equal(poller.poll(), true);
    await sleep(20);
    assert.equal(snapshot.fresh, true);
    assert.equal(snapshot.value.state, "PAUSED");
    assert.equal(paperStatusPresentation(snapshot.fresh, snapshot.value).stale, false);
  } finally {
    poller.stop();
  }
});
