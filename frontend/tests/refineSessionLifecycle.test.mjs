import assert from "node:assert/strict";
import test from "node:test";
import {
  addActiveSession,
  acquireRefineSession,
  canonicalizeJiraKey,
  hasActiveSession,
  removeActiveSession,
  RefineSessionCoordinator,
  refineSessionIdentity,
  shouldForgetReconnectMarker,
} from "../src/refineSessionLifecycle.ts";

test("reconnect identity is stable across Jira key casing", () => {
  assert.equal(canonicalizeJiraKey(" work-123 "), "WORK-123");
  assert.equal(refineSessionIdentity(42, "work-123"), "WORK-123");
});

test("two active sessions both reconnect after refresh", () => {
  const first = refineSessionIdentity(42, "work-123");
  const second = refineSessionIdentity(43, "work-124");
  let storageValue = null;

  storageValue = addActiveSession(storageValue, first);
  storageValue = addActiveSession(storageValue, second);

  assert.deepEqual(JSON.parse(storageValue), [first, second]);
  assert.equal(hasActiveSession(storageValue, first), true);
  assert.equal(hasActiveSession(storageValue, second), true);
  assert.equal(hasActiveSession(storageValue, first), true);
  assert.equal(hasActiveSession(storageValue, second), true);
});

test("closing either session preserves the other marker", () => {
  const first = refineSessionIdentity(42, "work-123");
  const second = refineSessionIdentity(43, "work-124");
  const storageValue = addActiveSession(addActiveSession(null, first), second);

  const afterFirstCloses = removeActiveSession(storageValue, first);
  assert.equal(hasActiveSession(afterFirstCloses, first), false);
  assert.equal(hasActiveSession(afterFirstCloses, second), true);

  const afterSecondCloses = removeActiveSession(storageValue, second);
  assert.equal(hasActiveSession(afterSecondCloses, first), true);
  assert.equal(hasActiveSession(afterSecondCloses, second), false);
});

test("reload and transient disconnects retain the reconnect marker", () => {
  assert.equal(shouldForgetReconnectMarker(false, true, false, 1006), false);
  assert.equal(shouldForgetReconnectMarker(false, false, false, 1006), false);
});

test("explicit close and confirmed session termination clear the marker", () => {
  assert.equal(shouldForgetReconnectMarker(true, false, false, 1006), true);
  assert.equal(shouldForgetReconnectMarker(false, false, true, 1006), true);
  assert.equal(shouldForgetReconnectMarker(false, false, false, 1000), true);
  assert.equal(shouldForgetReconnectMarker(false, false, false, 1011), true);
});

test("a marked subtask reconnects with a collapsed parent and shares its socket when shown", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = FakeWebSocket.OPEN;
    onmessage = null;
    onclose = null;
    constructor(url) {
      this.url = url;
      sockets.push(this);
    }
    send() {}
    close() {
      this.readyState = 3;
      this.onclose?.({ code: 1000 });
    }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const storage = {
    value: addActiveSession(null, refineSessionIdentity(2, "work-72")),
    getItem() { return this.value; },
    setItem(_key, value) { this.value = value; },
    removeItem() { this.value = null; },
  };
  const parent = { id: 1, jira_issue_key: "WORK-71", subtasks: [
    { id: 2, jira_issue_key: "work-72", subtasks: [], expanded: false },
  ], expanded: false };
  try {
    const firstMount = new RefineSessionCoordinator((id) => `/api/tickets/${id}/refine`, storage);
    firstMount.reconcile([parent]);
    assert.equal(sockets.length, 1);
    firstMount.dispose();

    const afterRefresh = new RefineSessionCoordinator((id) => `/api/tickets/${id}/refine`, storage);
    afterRefresh.reconcile([parent]);
    assert.equal(sockets.length, 2);

    const visibleTerminal = acquireRefineSession(
      refineSessionIdentity(2, "WORK-72"),
      "/api/tickets/2/refine",
      storage,
    );
    assert.equal(sockets.length, 2);
    visibleTerminal.release();
    afterRefresh.dispose();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("queues terminal input, replays initial output, and reconnects one Jira session", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = 0;
    onopen = null;
    onmessage = null;
    onclose = null;
    sent = [];
    constructor(url) {
      this.url = url;
      sockets.push(this);
    }
    send(data) {
      this.sent.push(data);
    }
    open() {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    }
    output(data) {
      this.onmessage?.({ data });
    }
    close() {
      this.readyState = 3;
      this.onclose?.({ code: 1006 });
    }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const storage = {
    value: addActiveSession(null, "WORK-73"),
    getItem() { return this.value; },
    setItem(_key, value) { this.value = value; },
    removeItem() { this.value = null; },
  };
  try {
    const first = acquireRefineSession("WORK-73", "/refine/73", storage);
    const output = [];
    first.subscribe((value) => output.push(value));
    first.resize(100, 30);
    first.send("before-open");
    assert.deepEqual(sockets[0].sent, []);
    sockets[0].open();
    assert.deepEqual(sockets[0].sent, [
      '{"type":"resize","cols":100,"rows":30}',
      '{"type":"input","data":"before-open"}',
    ]);
    sockets[0].output("initial output");
    const binaryOutput = new TextEncoder().encode("binary output").buffer;
    sockets[0].output(binaryOutput);
    assert.deepEqual(output, ["initial output", binaryOutput]);

    sockets[0].readyState = 3;
    sockets[0].onclose?.({ code: 1006 });
    assert.equal(sockets.length, 2);
    const second = acquireRefineSession("WORK-73", "/refine/73", storage);
    const replayed = [];
    second.subscribe((value) => replayed.push(value));
    assert.deepEqual(replayed, ["initial output", binaryOutput]);
    second.send("after-reconnect");
    assert.deepEqual(sockets[1].sent, []);
    sockets[1].open();
    assert.deepEqual(sockets[1].sent, ['{"type":"input","data":"after-reconnect"}']);
    first.release();
    second.release();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});
