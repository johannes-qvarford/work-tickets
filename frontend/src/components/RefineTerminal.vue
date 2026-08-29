<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import type { Ticket } from "./TicketCard.vue";
import {
  acquireRefineSession,
  activeSessionMarker,
  closeRefineSession,
  hasActiveSession,
  markActiveSession,
  refineSessionIdentity,
} from "../refineSessionLifecycle";

const props = defineProps<{
  ticket: Ticket;
  browserBaseUrl: string;
}>();

const visible = ref(false);
const terminalElement = ref<HTMLElement | null>(null);
let terminal: Terminal | null = null;
let unsubscribeOutput: (() => void) | null = null;
let sessionLease: ReturnType<typeof acquireRefineSession> | null = null;

function sessionIdentity() {
  return refineSessionIdentity(props.ticket.id, props.ticket.jira_issue_key);
}

function socketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/tickets/${props.ticket.id}/refine`;
}

function sessionStorageValue() {
  return activeSessionMarker();
}

async function openTerminal() {
  if (visible.value) return;
  markActiveSession(sessionIdentity());
  visible.value = true;
  await nextTick();
  if (!terminalElement.value) return;

  terminal = new Terminal({ convertEol: true, cursorBlink: true, scrollback: 5000 });
  terminal.open(terminalElement.value);
  terminal.write("Connecting to opencode...\r\n");
  sessionLease = acquireRefineSession(sessionIdentity(), socketUrl());
  unsubscribeOutput = sessionLease.subscribe((output) => terminal?.write(output));
  terminal.onData((data) => {
    sessionLease?.send(data);
  });
}

function releaseSession() {
  unsubscribeOutput?.();
  unsubscribeOutput = null;
  sessionLease?.release();
  sessionLease = null;
}

function closeTerminal() {
  closeRefineSession(sessionIdentity());
  releaseSession();
  terminal?.dispose();
  terminal = null;
  visible.value = false;
}

watch(visible, (isVisible) => {
  if (!isVisible) closeTerminal();
});
onMounted(() => {
  if (hasActiveSession(sessionStorageValue(), sessionIdentity())) {
    void openTerminal();
  }
});
onBeforeUnmount(() => {
  releaseSession();
  terminal?.dispose();
  terminal = null;
});
</script>

<template>
  <Button
    v-if="ticket.jira_issue_key"
    label="Refine"
    icon="pi pi-terminal"
    text
    :disabled="!ticket.component || !browserBaseUrl"
    :title="!ticket.component ? 'Assign a local component first' : (browserBaseUrl ? 'Open Refine terminal' : 'Configure the Jira browser URL first')"
    @click="openTerminal"
  />
  <Dialog v-model:visible="visible" modal :header="`Refine ${ticket.jira_issue_key || ticket.summary}`" :style="{ width: 'min(900px, 94vw)' }" @hide="closeTerminal">
    <div ref="terminalElement" class="refine-terminal" aria-label="Refine terminal"></div>
  </Dialog>
</template>
