<script setup lang="ts">
import { ref } from "vue";
import Button from "primevue/button";
import Card from "primevue/card";
import DatePicker from "primevue/datepicker";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";

export interface Ticket {
  id: number;
  parent_id: number | null;
  summary: string;
  description: string;
  planned_date: string | null;
  position: number;
  local_completed: boolean;
  jira_issue_key: string | null;
  jira_status_name: string | null;
  category_id: number | null;
  category_name: string | null;
  subtasks: Ticket[];
}

const props = defineProps<{ ticket: Ticket; categoryName: string; browserBaseUrl: string }>();
const emit = defineEmits<{
  toggle: [];
  sync: [];
  remove: [];
  save: [ticket: Ticket];
  saveSubtask: [subtask: Ticket];
  toggleSubtask: [id: number];
  removeSubtask: [id: number];
  addSubtask: [ticket: Ticket, draft: { summary: string; description: string; planned_date: string }];
}>();
const expanded = ref(false);
const draftSubtask = ref({ summary: "", description: "", planned_date: "" });

function todayValue() {
  const today = new Date();
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
}

function jiraIssueUrl(issueKey: string | null) {
  const browserBaseUrl = props.browserBaseUrl.trim().replace(/\/+$/, "");
  return browserBaseUrl && issueKey ? `${browserBaseUrl}/browse/${encodeURIComponent(issueKey)}` : null;
}

function saveDraftSubtask() {
  if (!draftSubtask.value.summary.trim()) return;
  emit("addSubtask", props.ticket, draftSubtask.value);
  draftSubtask.value = { summary: "", description: "", planned_date: "" };
}
</script>

<template>
  <Card :class="['ticket-card', ticket.local_completed && 'completed']">
    <template #content>
      <div class="ticket-title">
        <div>
          <span class="ticket-marker">{{ ticket.local_completed ? "✓" : "○" }}</span>
           <strong>{{ ticket.summary }}</strong>
           <a v-if="jiraIssueUrl(ticket.jira_issue_key)" class="jira-key" :href="jiraIssueUrl(ticket.jira_issue_key) || undefined" target="_blank" rel="noopener noreferrer">({{ ticket.jira_issue_key }})</a>
           <span v-else-if="ticket.jira_issue_key" class="jira-key">({{ ticket.jira_issue_key }})</span>
          <div class="ticket-meta">
            {{ categoryName }} · {{ ticket.planned_date || "Unscheduled" }}
            <span v-if="ticket.jira_status_name"> · Jira: {{ ticket.jira_status_name }}</span>
          </div>
        </div>
        <div class="button-row">
          <Button
            :icon="ticket.local_completed ? 'pi pi-undo' : 'pi pi-check'"
            text rounded
            :aria-label="ticket.local_completed ? 'Mark active' : 'Mark done'"
            @click="emit('toggle')"
          />
          <Button v-if="!ticket.local_completed && ticket.jira_issue_key" icon="pi pi-cloud-download" text rounded aria-label="Sync from Jira" @click="emit('sync')" />
          <Button v-else-if="!ticket.local_completed" icon="pi pi-sync" text rounded aria-label="Sync to Jira" @click="emit('sync')" />
          <Button v-if="!ticket.local_completed" icon="pi pi-trash" severity="danger" text rounded aria-label="Delete ticket" @click="emit('remove')" />
        </div>
      </div>
      <Button
        v-if="!ticket.local_completed || ticket.subtasks.length"
        class="edit-toggle"
        :label="expanded ? 'Hide details' : (ticket.local_completed ? 'View subtasks' : 'Edit ticket and subtasks')"
        :icon="expanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
        text
        @click="expanded = !expanded"
      />
      <div v-if="expanded" class="form details-form">
        <template v-if="!ticket.local_completed">
          <InputText v-model="ticket.summary" aria-label="Ticket summary" />
          <div class="date-control"><InputText v-model="ticket.planned_date" type="date" aria-label="Planned date" /><Button type="button" label="Today" text aria-label="Set planned date to today" @click="ticket.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove planned date" :disabled="!ticket.planned_date" @click="ticket.planned_date = null" /></div>
          <Textarea v-model="ticket.description" rows="3" autoResize aria-label="Ticket description" />
          <span class="ticket-meta">Category: {{ categoryName }}</span>
          <Button label="Save ticket" @click="emit('save', ticket)" />
        </template>
        <span v-else class="ticket-meta">Done items can only be marked active.</span>
         <h4>Subtasks ({{ ticket.subtasks.length }})</h4>
         <div v-for="subtask in ticket.subtasks" :key="subtask.id" class="subtask-row">
            <a v-if="jiraIssueUrl(subtask.jira_issue_key)" class="jira-key" :href="jiraIssueUrl(subtask.jira_issue_key) || undefined" target="_blank" rel="noopener noreferrer">({{ subtask.jira_issue_key }})</a>
            <span v-else-if="subtask.jira_issue_key" class="jira-key">({{ subtask.jira_issue_key }})</span>
            <InputText v-if="!subtask.local_completed" v-model="subtask.summary" :aria-label="`Subtask ${subtask.summary}`" />
            <span v-else class="ticket-meta subtask-completed">{{ subtask.summary }}</span>
           <div v-if="!subtask.local_completed" class="date-control">
             <InputText v-model="subtask.planned_date" type="date" aria-label="Subtask planned date" />
             <Button type="button" label="Today" text aria-label="Set subtask planned date to today" @click="subtask.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove subtask planned date" :disabled="!subtask.planned_date" @click="subtask.planned_date = null" />
           </div>
           <Button :icon="subtask.local_completed ? 'pi pi-undo' : 'pi pi-check'" text @click="emit('toggleSubtask', subtask.id)" />
           <Button v-if="!subtask.local_completed" icon="pi pi-trash" severity="danger" text @click="emit('removeSubtask', subtask.id)" />
           <Button v-if="!subtask.local_completed" label="Save" text @click="emit('saveSubtask', subtask)" />
         </div>
         <div v-if="!ticket.local_completed" class="subtask-row">
           <InputText v-model="draftSubtask.summary" placeholder="New subtask" />
           <div class="date-control">
             <InputText v-model="draftSubtask.planned_date" type="date" aria-label="New subtask planned date" />
             <Button type="button" label="Today" text aria-label="Set new subtask planned date to today" @click="draftSubtask.planned_date = todayValue()" /><Button type="button" label="Unfocus" text aria-label="Remove new subtask planned date" :disabled="!draftSubtask.planned_date" @click="draftSubtask.planned_date = ''" />
           </div>
           <Button label="Add" icon="pi pi-plus" @click="saveDraftSubtask" />
         </div>
      </div>
    </template>
  </Card>
</template>
