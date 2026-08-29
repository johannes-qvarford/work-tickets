<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import type { Ticket } from "./TicketCard.vue";

const props = defineProps<{
  ticket: Ticket;
  browserBaseUrl: string;
}>();

const visible = ref(false);
const terminalElement = ref<HTMLElement | null>(null);
let terminal: Terminal | null = null;
let socket: WebSocket | null = null;

function socketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/tickets/${props.ticket.id}/refine`;
}

async function openTerminal() {
  visible.value = true;
  await nextTick();
  if (!terminalElement.value) return;

  terminal = new Terminal({ convertEol: true, cursorBlink: true, scrollback: 5000 });
  terminal.open(terminalElement.value);
  terminal.write("Connecting to opencode...\r\n");
  socket = new WebSocket(socketUrl());
  socket.onmessage = (event) => terminal?.write(String(event.data));
  socket.onerror = () => terminal?.write("\r\n[Refine connection failed]\r\n");
  socket.onclose = () => {
    socket = null;
    terminal?.write("\r\n[Refine connection closed]\r\n");
  };
  terminal.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(data);
  });
}

function closeTerminal() {
  socket?.close();
  socket = null;
  terminal?.dispose();
  terminal = null;
  visible.value = false;
}

watch(visible, (isVisible) => {
  if (!isVisible) closeTerminal();
});
onBeforeUnmount(closeTerminal);
</script>

<template>
  <Button
    v-if="ticket.jira_issue_key"
    label="Refine"
    icon="pi pi-terminal"
    text
    :disabled="!browserBaseUrl"
    :title="browserBaseUrl ? 'Open Refine terminal' : 'Configure the Jira browser URL first'"
    @click="openTerminal"
  />
  <Dialog v-model:visible="visible" modal :header="`Refine ${ticket.jira_issue_key || ticket.summary}`" :style="{ width: 'min(900px, 94vw)' }" @hide="closeTerminal">
    <div ref="terminalElement" class="refine-terminal" aria-label="Refine terminal"></div>
  </Dialog>
</template>
