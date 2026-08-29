<script setup lang="ts">
import { onBeforeUnmount, watch } from "vue";
import type { Ticket } from "./TicketCard.vue";
import {
  RefineSessionCoordinator as SessionCoordinator,
  type RefineTicketLifecycle,
} from "../refineSessionLifecycle";

const props = defineProps<{ tickets: Ticket[] }>();

function socketUrl(ticketId: number): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/tickets/${ticketId}/refine`;
}

const coordinator = new SessionCoordinator(socketUrl);
watch(
  () => props.tickets,
  (tickets) => coordinator.reconcile(tickets as RefineTicketLifecycle[]),
  { immediate: true, deep: true },
);
onBeforeUnmount(() => coordinator.dispose());
</script>

<template>
  <span hidden aria-hidden="true"></span>
</template>
