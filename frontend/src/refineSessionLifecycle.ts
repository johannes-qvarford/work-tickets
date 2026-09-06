export function canonicalizeJiraKey(key: string): string {
  return key.trim().toUpperCase();
}

export function refineSessionIdentity(ticketId: number, jiraIssueKey: string | null): string {
  void ticketId;
  return canonicalizeJiraKey(jiraIssueKey || "");
}

export const activeSessionStorageKey = "work-tickets-active-refine";

export interface SessionStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function browserSessionStorage(): SessionStorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function parseActiveSessionIdentities(storageValue: string | null): string[] {
  if (!storageValue) return [];
  try {
    const parsed: unknown = JSON.parse(storageValue);
    if (Array.isArray(parsed)) {
      return [...new Set(parsed.filter((identity): identity is string => typeof identity === "string"))];
    }
  } catch {
    // Treat the previous scalar marker format as one active session.
  }
  return [storageValue];
}

export function hasActiveSession(storageValue: string | null, identity: string): boolean {
  return parseActiveSessionIdentities(storageValue).includes(identity);
}

export function addActiveSession(storageValue: string | null, identity: string): string {
  return JSON.stringify([...new Set([...parseActiveSessionIdentities(storageValue), identity])]);
}

export function removeActiveSession(storageValue: string | null, identity: string): string | null {
  const remaining = parseActiveSessionIdentities(storageValue).filter((item) => item !== identity);
  return remaining.length ? JSON.stringify(remaining) : null;
}

export function shouldForgetReconnectMarker(
  explicitClose: boolean,
  unmounting: boolean,
  confirmedSessionEnd: boolean,
  closeCode: number,
): boolean {
  if (explicitClose) return true;
  if (unmounting) return false;
  return confirmedSessionEnd || closeCode === 1000 || closeCode === 1011;
}

export function activeSessionMarker(storage = browserSessionStorage()): string | null {
  try {
    return storage?.getItem(activeSessionStorageKey) || null;
  } catch {
    return null;
  }
}

export function markActiveSession(
  identity: string,
  storage = browserSessionStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(
      activeSessionStorageKey,
      addActiveSession(activeSessionMarker(storage), identity),
    );
  } catch {
    // Storage may be unavailable.
  }
}

export function forgetActiveSession(
  identity: string,
  storage = browserSessionStorage(),
): void {
  if (!storage) return;
  try {
    const nextValue = removeActiveSession(activeSessionMarker(storage), identity);
    if (nextValue === null) storage.removeItem(activeSessionStorageKey);
    else storage.setItem(activeSessionStorageKey, nextValue);
  } catch {
    // Storage may be unavailable.
  }
}

export type RefineTerminalOutput = string | ArrayBuffer;
type OutputListener = (output: RefineTerminalOutput) => void;

interface Connection {
  identity: string;
  url: string;
  storage: SessionStorageLike | null;
  socket: WebSocket | null;
  socketTransientClose: boolean;
  socketExplicitClose: boolean;
  ended: boolean;
  leases: number;
  output: RefineTerminalOutput[];
  pendingInput: string;
  pendingResize: { cols: number; rows: number } | null;
  listeners: Set<OutputListener>;
}

const connections = new Map<string, Connection>();

function connect(connection: Connection): void {
  const socket = new WebSocket(connection.url);
  connection.socket = socket;
  socket.binaryType = "arraybuffer";
  connection.socketTransientClose = false;
  connection.socketExplicitClose = false;
  socket.onopen = () => {
    if (connection.socket !== socket || socket.readyState !== WebSocket.OPEN) return;
    const pendingInput = connection.pendingInput;
    connection.pendingInput = "";
    if (connection.pendingResize) {
      socket.send(JSON.stringify({ type: "resize", ...connection.pendingResize }));
      connection.pendingResize = null;
    }
    if (pendingInput) socket.send(JSON.stringify({ type: "input", data: pendingInput }));
  };
  const publishOutput = (output: RefineTerminalOutput) => {
    connection.output.push(output);
    if (connection.output.length > 512) connection.output.shift();
    if (
      typeof output === "string" &&
      (output.includes("[Refine exited with code") || output.includes("[Refine error]"))
    ) {
      connection.ended = true;
      forgetActiveSession(connection.identity, connection.storage);
    }
    for (const listener of connection.listeners) listener(output);
  };
  socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      publishOutput(event.data);
    } else if (typeof Blob !== "undefined" && event.data instanceof Blob) {
      void event.data.arrayBuffer().then(publishOutput);
    } else {
      publishOutput(String(event.data));
    }
  };
  socket.onclose = (event) => {
    const wasTransientClose = connection.socketTransientClose;
    const wasExplicitClose = connection.socketExplicitClose;
    const sessionEnded = shouldForgetReconnectMarker(
      wasExplicitClose,
      wasTransientClose,
      connection.ended,
      event.code,
    );
    if (sessionEnded) {
      connection.ended = true;
      forgetActiveSession(connection.identity, connection.storage);
    }
    if (connection.socket !== socket) return;
    connection.socket = null;
    if (connection.leases > 0 && !connection.ended && !wasExplicitClose) {
      connect(connection);
    } else {
      connections.delete(connection.identity);
    }
  };
}

export interface RefineSessionLease {
  subscribe(listener: OutputListener): () => void;
  send(data: string): void;
  resize(cols: number, rows: number): void;
  release(): void;
}

export function acquireRefineSession(
  identity: string,
  url: string,
  storage = browserSessionStorage(),
): RefineSessionLease {
  let connection = connections.get(identity);
  if (!connection) {
    connection = {
      identity,
      url,
      storage,
       socket: null,
      socketTransientClose: false,
      socketExplicitClose: false,
      ended: false,
      leases: 0,
       output: [],
       pendingInput: "",
       pendingResize: null,
       listeners: new Set(),
    };
    connections.set(identity, connection);
    connect(connection);
  }
  connection.leases += 1;
  let released = false;
  return {
    subscribe(listener) {
      for (const output of connection.output) listener(output);
      connection.listeners.add(listener);
      return () => connection?.listeners.delete(listener);
    },
    send(data) {
      if (!data || connection.ended) return;
      if (connection.socket?.readyState === WebSocket.OPEN) {
        connection.socket.send(JSON.stringify({ type: "input", data }));
      }
      else connection.pendingInput += data;
    },
    resize(cols, rows) {
      if (connection.ended) return;
      const size = { cols, rows };
      if (connection.socket?.readyState === WebSocket.OPEN) {
        connection.socket.send(JSON.stringify({ type: "resize", ...size }));
      } else {
        connection.pendingResize = size;
      }
    },
    release() {
      if (released) return;
      released = true;
      connection.leases -= 1;
      if (connection.leases === 0) {
        if (connection.socket) {
          connection.socketTransientClose = true;
          connection.socket.close();
        } else {
          connections.delete(connection.identity);
        }
      }
    },
  };
}

export function closeRefineSession(
  identity: string,
  storage = browserSessionStorage(),
): void {
  forgetActiveSession(identity, storage);
  const connection = connections.get(identity);
  if (!connection) return;
  connection.ended = true;
  connection.socketExplicitClose = true;
  connections.delete(identity);
  connection.socket?.close();
}

export interface RefineTicketLifecycle {
  id: number;
  jira_issue_key: string | null;
  subtasks: RefineTicketLifecycle[];
}

export class RefineSessionCoordinator {
  private readonly leases = new Map<string, RefineSessionLease>();
  private readonly socketUrl: (ticketId: number) => string;
  private readonly storage: SessionStorageLike | null;

  constructor(
    socketUrl: (ticketId: number) => string,
    storage: SessionStorageLike | null = browserSessionStorage(),
  ) {
    this.socketUrl = socketUrl;
    this.storage = storage;
  }

  reconcile(tickets: RefineTicketLifecycle[]): void {
    const marked = new Map<string, number>();
    const visit = (ticket: RefineTicketLifecycle) => {
      if (ticket.jira_issue_key) {
        const identity = refineSessionIdentity(ticket.id, ticket.jira_issue_key);
        if (hasActiveSession(activeSessionMarker(this.storage), identity)) {
          marked.set(identity, ticket.id);
        }
      }
      for (const subtask of ticket.subtasks) visit(subtask);
    };
    for (const ticket of tickets) visit(ticket);

    for (const [identity, lease] of this.leases) {
      if (!marked.has(identity)) {
        lease.release();
        this.leases.delete(identity);
      }
    }
    for (const [identity, ticketId] of marked) {
      if (!this.leases.has(identity)) {
        this.leases.set(
          identity,
          acquireRefineSession(identity, this.socketUrl(ticketId), this.storage),
        );
      }
    }
  }

  dispose(): void {
    for (const lease of this.leases.values()) lease.release();
    this.leases.clear();
  }
}
