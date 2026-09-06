import assert from "node:assert/strict";
import test from "node:test";
import {
  addActiveSession,
  acquireRefineSession,
  acquireImplementSession,
  activeImplementSessionMarker,
  canonicalizeJiraKey,
  closeImplementSession,
  markActiveImplementSession,
  hasActiveSession,
  removeActiveSession,
  RefineSessionCoordinator,
  ImplementSessionCoordinator,
  refineSessionIdentity,
  implementSessionIdentity,
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

test("Refine and Implement use independent identities, markers, and sockets", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = FakeWebSocket.OPEN;
    onmessage = null;
    onclose = null;
    constructor(url) { this.url = url; sockets.push(this); }
    send() {}
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) ?? null; },
    setItem(key, value) { this.values.set(key, value); },
    removeItem(key) { this.values.delete(key); },
  };
  try {
    const refine = acquireRefineSession(refineSessionIdentity(42, "work-123"), "/refine", storage);
    const implement = acquireImplementSession(implementSessionIdentity(42, "work-123"), "/implement", storage);
    assert.equal(sockets.length, 2);
    assert.equal(sockets[0].url, "/refine");
    assert.equal(sockets[1].url, "/implement");
    refine.release();
    assert.equal(sockets[1].readyState, FakeWebSocket.OPEN);
    implement.release();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("Implement reconnects after a disconnect and explicit close clears its marker", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = FakeWebSocket.OPEN;
    onmessage = null;
    onclose = null;
    constructor(url) { this.url = url; sockets.push(this); }
    send() {}
    close() { this.readyState = 3; this.onclose?.({ code: 1006 }); }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) ?? null; },
    setItem(key, value) { this.values.set(key, value); },
    removeItem(key) { this.values.delete(key); },
  };
  const identity = implementSessionIdentity(44, "work-144");
  try {
    markActiveImplementSession(identity, storage);
    const lease = acquireImplementSession(identity, "/implement/44", storage);
    sockets[0].onclose?.({ code: 1006 });
    assert.equal(sockets.length, 2);
    assert.equal(activeImplementSessionMarker(storage), JSON.stringify([identity]));

    closeImplementSession(identity, storage);
    assert.equal(activeImplementSessionMarker(storage), null);
    assert.equal(sockets[1].readyState, 3);
    lease.release();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("Implement startup errors end the session without reconnecting", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = FakeWebSocket.OPEN;
    onmessage = null;
    onclose = null;
    constructor(url) { this.url = url; sockets.push(this); }
    send() {}
    output(data) { this.onmessage?.({ data }); }
    close() { this.readyState = 3; this.onclose?.({ code: 1011 }); }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) ?? null; },
    setItem(key, value) { this.values.set(key, value); },
    removeItem(key) { this.values.delete(key); },
  };
  const identity = implementSessionIdentity(47, "work-147");
  try {
    markActiveImplementSession(identity, storage);
    const lease = acquireImplementSession(identity, "/implement/47", storage);
    sockets[0].output("\r\n[Implement error] Could not start opencode\r\n");
    assert.equal(activeImplementSessionMarker(storage), null);
    sockets[0].onclose?.({ code: 1011 });
    assert.equal(sockets.length, 1);
    lease.release();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
});

test("Implement coordinator reconnects a marked child independently", () => {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    readyState = FakeWebSocket.OPEN;
    onmessage = null;
    onclose = null;
    constructor(url) { this.url = url; sockets.push(this); }
    send() {}
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = FakeWebSocket;
  const childIdentity = implementSessionIdentity(46, "work-146");
  const storage = {
    value: addActiveSession(null, childIdentity),
    getItem() { return this.value; },
    setItem(_key, value) { this.value = value; },
    removeItem() { this.value = null; },
  };
  const parent = {
    id: 45,
    jira_issue_key: "WORK-145",
    subtasks: [{ id: 46, jira_issue_key: "work-146", subtasks: [] }],
  };
  try {
    const coordinator = new ImplementSessionCoordinator(
      (id) => `/api/tickets/${id}/implement`,
      storage,
    );
    coordinator.reconcile([parent]);
    assert.equal(sockets.length, 1);
    assert.equal(sockets[0].url, "/api/tickets/46/implement");
    coordinator.dispose();
  } finally {
    globalThis.WebSocket = previousWebSocket;
  }
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
