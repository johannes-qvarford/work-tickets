<script setup lang="ts">
import { computed, ref } from "vue";
import Button from "primevue/button";
import Card from "primevue/card";
import DatePicker from "primevue/datepicker";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import CategoryButtons, { type Category, type CategoryComponent } from "./CategoryButtons.vue";
import ComponentSelect from "./ComponentSelect.vue";
import RefineTerminal from "./RefineTerminal.vue";
import { canonicalizeJiraKey } from "../refineSessionLifecycle";
import {
  clearSubtaskDragState,
  clearSubtaskDragOverState,
  dragOverSubtaskId,
  dragOverSubtaskParentId,
  draggingSubtaskId,
  draggingSubtaskParentId,
  dropTargetIndex,
} from "../reordering";

export interface Ticket {
  id: number;
  parent_id: number | null;
  summary: string;
  description: string;
  notes?: string;
  planned_date: string | null;
  position: number;
  local_completed: boolean;
  jira_issue_key: string | null;
  jira_status_name: string | null;
  category_id: number | null;
  category_name: string | null;
  component: string | null;
  subtasks: Ticket[];
}

const props = defineProps<{
  ticket: Ticket;
  categories: Category[];
  components: CategoryComponent[];
  categoryName: string;
  browserBaseUrl: string;
  reorderable?: boolean;
  draggingTicketId?: number | null;
  dragOverTicketId?: number | null;
}>();
const emit = defineEmits<{
  toggle: [];
  sync: [];
  remove: [];
  save: [ticket: Ticket];
  saveSubtask: [subtask: Ticket];
  toggleSubtask: [id: number];
  removeSubtask: [id: number];
  addSubtask: [ticket: Ticket, draft: { summary: string; description: string; planned_date: string }];
  ticketDragStart: [id: number];
  ticketDragEnd: [];
  ticketDragOver: [id: number];
  ticketDrop: [id: number, afterTarget: boolean];
  moveSubtask: [id: number, targetIndex: number];
}>();
const expanded = ref(false);
const draftSubtask = ref({ summary: "", description: "", planned_date: "" });
const activeSubtasks = computed(() => props.ticket.subtasks.filter((subtask) => !subtask.local_completed));

const ticketCanDrag = computed(() => Boolean(props.reorderable && !props.ticket.local_completed));

function todayValue() {
  const today = new Date();
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
}

function jiraIssueUrl(issueKey: string | null) {
  const browserBaseUrl = props.browserBaseUrl.trim().replace(/\/+$/, "");
  return browserBaseUrl && issueKey
    ? `${browserBaseUrl}/browse/${encodeURIComponent(canonicalizeJiraKey(issueKey))}`
    : null;
}

function saveDraftSubtask() {
  if (!draftSubtask.value.summary.trim()) return;
  emit("addSubtask", props.ticket, draftSubtask.value);
  draftSubtask.value = { summary: "", description: "", planned_date: "" };
}

function onTicketDragStart(event: DragEvent) {
  if (!ticketCanDrag.value || !event.dataTransfer) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("application/x-work-tickets-ticket", String(props.ticket.id));
  emit("ticketDragStart", props.ticket.id);
}

function isTicketDrag(event: DragEvent) {
  return event.dataTransfer?.types.includes("application/x-work-tickets-ticket") ?? false;
}

function onTicketDragOver(event: DragEvent) {
  if (isSubtaskDrag(event)) {
    clearSubtaskDragOverState();
    return;
  }
  if (!ticketCanDrag.value || !isTicketDrag(event) || props.draggingTicketId === props.ticket.id) return;
  event.preventDefault();
  event.stopPropagation();
  event.dataTransfer!.dropEffect = "move";
  emit("ticketDragOver", props.ticket.id);
}

function onTicketDrop(event: DragEvent) {
  if (isSubtaskDrag(event)) {
    event.preventDefault();
    event.stopPropagation();
    clearSubtaskDragState();
    return;
  }
  if (!isTicketDrag(event)) return;
  if (!ticketCanDrag.value || props.draggingTicketId === null || props.draggingTicketId === undefined || props.draggingTicketId === props.ticket.id) {
    event.preventDefault();
    event.stopPropagation();
    emit("ticketDragEnd");
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const target = event.currentTarget as HTMLElement;
  const afterTarget = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
  emit("ticketDrop", props.ticket.id, afterTarget);
}

function onSubtaskDragStart(event: DragEvent, subtask: Ticket) {
  if (subtask.local_completed || !event.dataTransfer) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("application/x-work-tickets-subtask", String(subtask.id));
  draggingSubtaskParentId.value = props.ticket.id;
  draggingSubtaskId.value = subtask.id;
  dragOverSubtaskParentId.value = null;
  dragOverSubtaskId.value = null;
}

function isSubtaskDrag(event: DragEvent) {
  return event.dataTransfer?.types.includes("application/x-work-tickets-subtask") ?? false;
}

function onSubtaskDragOver(event: DragEvent, subtask: Ticket) {
  if (!isSubtaskDrag(event)) return;
  if (subtask.local_completed || draggingSubtaskParentId.value !== props.ticket.id || draggingSubtaskId.value === subtask.id) {
    clearSubtaskDragOverState();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  event.dataTransfer!.dropEffect = "move";
  dragOverSubtaskParentId.value = props.ticket.id;
  dragOverSubtaskId.value = subtask.id;
}

function onSubtaskDrop(event: DragEvent, subtask: Ticket) {
  if (!isSubtaskDrag(event)) return;
  if (subtask.local_completed || draggingSubtaskParentId.value !== props.ticket.id || draggingSubtaskId.value === subtask.id) {
    event.preventDefault();
    event.stopPropagation();
    clearSubtaskDragState();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const target = event.currentTarget as HTMLElement;
  const afterTarget = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
  const sourceId = draggingSubtaskId.value;
  const targetIndex = sourceId === null ? null : dropTargetIndex(activeSubtasks.value, sourceId, subtask.id, afterTarget);
  clearSubtaskDragState();
  if (targetIndex !== null && sourceId !== null) {
    emit("moveSubtask", sourceId, targetIndex);
  }
}

</script>

<template>
  <Card
    :class="['ticket-card', ticket.local_completed && 'completed', draggingTicketId === ticket.id && 'dragging', dragOverTicketId === ticket.id && 'drag-over']"
    @dragover="onTicketDragOver"
    @drop="onTicketDrop"
  >
    <template #content>
      <div class="ticket-title">
        <button
          v-if="ticketCanDrag"
          type="button"
          class="drag-handle ticket-drag-handle"
          draggable="true"
          :aria-label="`Drag to reorder ${ticket.summary}`"
          title="Drag to reorder"
          @dragstart="onTicketDragStart"
          @dragend="emit('ticketDragEnd')"
        >⠿</button>
        <div class="ticket-summary">
          <strong>{{ ticket.summary }}</strong>
          <a v-if="jiraIssueUrl(ticket.jira_issue_key)" class="jira-key" :href="jiraIssueUrl(ticket.jira_issue_key) || undefined" target="_blank" rel="noopener noreferrer">({{ ticket.jira_issue_key }})</a>
          <span v-else-if="ticket.jira_issue_key" class="jira-key">({{ ticket.jira_issue_key }})</span>
          <div class="ticket-meta">
            {{ categoryName }} · {{ ticket.planned_date || "Unscheduled" }}
            <span v-if="ticket.jira_status_name"> · Jira: {{ ticket.jira_status_name }}</span>
          </div>
        </div>
        <div class="button-row">
          <Button :icon="ticket.local_completed ? 'pi pi-undo' : 'pi pi-check'" text rounded :aria-label="ticket.local_completed ? 'Mark active' : 'Mark done'" @click="emit('toggle')" />
          <RefineTerminal :ticket="ticket" :browser-base-url="browserBaseUrl" />
          <Button v-if="!ticket.local_completed && ticket.jira_issue_key" icon="pi pi-cloud-download" text rounded aria-label="Sync from Jira" @click="emit('sync')" />
          <Button v-else-if="!ticket.local_completed" icon="pi pi-sync" text rounded aria-label="Sync to Jira" @click="emit('sync')" />
          <Button v-if="!ticket.local_completed" icon="pi pi-trash" severity="danger" text rounded aria-label="Delete ticket" @click="emit('remove')" />
        </div>
      </div>
      <Button v-if="!ticket.local_completed || ticket.subtasks.length" class="edit-toggle" :label="expanded ? 'Hide details' : (ticket.local_completed ? 'View subtasks' : 'Edit ticket and subtasks')" :icon="expanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" text @click="expanded = !expanded" />
      <div v-if="expanded" class="form details-form">
        <template v-if="!ticket.local_completed">
          <InputText v-model="ticket.summary" aria-label="Ticket summary" />
          <div class="date-control"><InputText v-model="ticket.planned_date" type="date" aria-label="Planned date" /><Button type="button" label="Today" text aria-label="Set planned date to today" @click="ticket.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove planned date" :disabled="!ticket.planned_date" @click="ticket.planned_date = null" /></div>
          <Textarea v-model="ticket.description" rows="3" autoResize aria-label="Ticket description" />
          <Textarea v-if="ticket.parent_id === null" v-model="ticket.notes" rows="4" autoResize aria-label="Personal notes" placeholder="Notes for your local workflow" />
          <label class="category-field">Category<CategoryButtons v-model="ticket.category_id" :categories="categories" /></label>
          <label class="component-field">Component<ComponentSelect v-model="ticket.component" :categories="categories" :components="components" :category-id="ticket.category_id" /></label>
          <Button label="Save ticket" @click="emit('save', ticket)" />
        </template>
        <span v-else class="ticket-meta">Done items can only be marked active.</span>
        <div class="subtasks-heading"><h4>Subtasks ({{ ticket.subtasks.length }})</h4><span class="ticket-meta">Active subtasks can be dragged to reorder.</span></div>
        <div class="subtask-list">
          <div
            v-for="subtask in ticket.subtasks"
            :key="subtask.id"
            :class="['subtask-row', subtask.local_completed && 'completed', draggingSubtaskParentId === ticket.id && draggingSubtaskId === subtask.id && 'dragging', dragOverSubtaskParentId === ticket.id && dragOverSubtaskId === subtask.id && 'drag-over']"
            @dragover="onSubtaskDragOver($event, subtask)"
            @drop="onSubtaskDrop($event, subtask)"
          >
            <button v-if="!subtask.local_completed" type="button" class="drag-handle subtask-drag-handle" draggable="true" :aria-label="`Drag to reorder ${subtask.summary}`" title="Drag to reorder" @dragstart="onSubtaskDragStart($event, subtask)" @dragend="clearSubtaskDragState">⠿</button>
            <a v-if="jiraIssueUrl(subtask.jira_issue_key)" class="jira-key" :href="jiraIssueUrl(subtask.jira_issue_key) || undefined" target="_blank" rel="noopener noreferrer">({{ subtask.jira_issue_key }})</a>
            <span v-else-if="subtask.jira_issue_key" class="jira-key">({{ subtask.jira_issue_key }})</span>
            <RefineTerminal :ticket="subtask" :browser-base-url="browserBaseUrl" />
            <InputText v-if="!subtask.local_completed" v-model="subtask.summary" :aria-label="`Subtask ${subtask.summary}`" />
            <span v-else class="ticket-meta subtask-completed">{{ subtask.summary }}</span>
            <div v-if="!subtask.local_completed" class="date-control">
              <InputText v-model="subtask.planned_date" type="date" aria-label="Subtask planned date" />
              <Button type="button" label="Today" text aria-label="Set subtask planned date to today" @click="subtask.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove subtask planned date" :disabled="!subtask.planned_date" @click="subtask.planned_date = null" />
            </div>
            <Button :icon="subtask.local_completed ? 'pi pi-undo' : 'pi pi-check'" text :aria-label="subtask.local_completed ? 'Mark subtask active' : 'Mark subtask done'" @click="emit('toggleSubtask', subtask.id)" />
            <Button v-if="!subtask.local_completed" icon="pi pi-trash" severity="danger" text aria-label="Delete subtask" @click="emit('removeSubtask', subtask.id)" />
            <Button v-if="!subtask.local_completed" label="Save" text @click="emit('saveSubtask', subtask)" />
          </div>
        </div>
        <div v-if="!ticket.local_completed" class="subtask-row">
          <InputText v-model="draftSubtask.summary" placeholder="New subtask" />
          <div class="date-control"><InputText v-model="draftSubtask.planned_date" type="date" aria-label="New subtask planned date" /><Button type="button" label="Today" text aria-label="Set new subtask planned date to today" @click="draftSubtask.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove new subtask planned date" :disabled="!draftSubtask.planned_date" @click="draftSubtask.planned_date = ''" /></div>
          <Button label="Add" icon="pi pi-plus" @click="saveDraftSubtask" />
        </div>
      </div>
    </template>
  </Card>
</template>
