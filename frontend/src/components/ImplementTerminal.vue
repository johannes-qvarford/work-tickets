<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { Ticket } from "./TicketCard.vue";
import {
  acquireImplementSession,
  activeImplementSessionMarker,
  closeImplementSession,
  hasActiveImplementSession,
  implementSessionIdentity,
  markActiveImplementSession,
  type RefineTerminalOutput,
} from "../refineSessionLifecycle";

const props = defineProps<{ ticket: Ticket; browserBaseUrl: string }>();
const visible = ref(false);
const statusMessage = ref("OpenCode runs in the ticket's local project.");
const terminalElement = ref<HTMLElement | null>(null);
let terminal: Terminal | null = null;
let fitAddon: FitAddon | null = null;
let unsubscribeOutput: (() => void) | null = null;
let sessionLease: ReturnType<typeof acquireImplementSession> | null = null;
let resizeObserver: ResizeObserver | null = null;
let resizeFrame: number | null = null;

function sessionIdentity() { return implementSessionIdentity(props.ticket.id, props.ticket.jira_issue_key); }
function socketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ cols: String(terminal?.cols || 80), rows: String(terminal?.rows || 24) });
  return `${protocol}//${window.location.host}/api/tickets/${props.ticket.id}/implement?${query}`;
}
function writeOutput(output: RefineTerminalOutput) { if (typeof output === "string") terminal?.write(output); else terminal?.write(new Uint8Array(output)); }
function sendResize() { if (terminal && sessionLease) sessionLease.resize(terminal.cols, terminal.rows); }
function fitTerminalNow() { fitAddon?.fit(); sendResize(); }
function scheduleFit() {
  if (resizeFrame !== null) return;
  resizeFrame = requestAnimationFrame(() => { resizeFrame = null; fitTerminalNow(); });
}
async function openTerminal() {
  if (visible.value) return;
  markActiveImplementSession(sessionIdentity());
  visible.value = true;
  await nextTick();
  if (!terminalElement.value) return;
  terminal = new Terminal({ convertEol: true, cursorBlink: true, scrollback: 5000 });
  fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(terminalElement.value);
  fitTerminalNow();
  sessionLease = acquireImplementSession(sessionIdentity(), socketUrl());
  unsubscribeOutput = sessionLease.subscribe(writeOutput);
  sendResize();
  terminal.onData((data) => sessionLease?.send(data));
  resizeObserver = new ResizeObserver(scheduleFit);
  resizeObserver.observe(terminalElement.value);
}
function releaseSession() { unsubscribeOutput?.(); unsubscribeOutput = null; sessionLease?.release(); sessionLease = null; }
function closeTerminal() {
  if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
  resizeFrame = null;
  resizeObserver?.disconnect(); resizeObserver = null;
  closeImplementSession(sessionIdentity()); releaseSession(); terminal?.dispose();
  terminal = null; fitAddon = null; statusMessage.value = "OpenCode runs in the ticket's local project."; visible.value = false;
}
watch(visible, (isVisible) => { if (!isVisible) closeTerminal(); });
onMounted(() => { if (hasActiveImplementSession(activeImplementSessionMarker(), sessionIdentity())) void openTerminal(); });
onBeforeUnmount(() => { releaseSession(); resizeObserver?.disconnect(); resizeObserver = null; terminal?.dispose(); terminal = null; fitAddon = null; });
</script>

<template>
  <Button v-if="ticket.jira_issue_key" label="Implement" icon="pi pi-terminal" text :disabled="!ticket.component || !browserBaseUrl" :title="!ticket.component ? 'Assign a local component first' : (browserBaseUrl ? 'Open Implement terminal' : 'Configure the Jira browser URL first')" @click="openTerminal" />
  <Dialog v-model:visible="visible" modal :header="`Implement ${ticket.jira_issue_key || ticket.summary}`" :style="{ width: 'min(900px, 94vw)' }" @hide="closeTerminal">
    <p class="muted">{{ statusMessage }} Type into the terminal to interact with OpenCode.</p>
    <div ref="terminalElement" class="refine-terminal" aria-label="Implement terminal"></div>
  </Dialog>
</template>
