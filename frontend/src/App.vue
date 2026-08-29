<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import Button from "primevue/button";
import Card from "primevue/card";
import DatePicker from "primevue/datepicker";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import Select from "primevue/select";
import Textarea from "primevue/textarea";
import TicketCard, { type Ticket } from "./components/TicketCard.vue";

interface Category { id: number; name: string }
interface JiraConfig { base_url: string; browser_base_url: string; email: string; project_key: string; issue_type: string; completed_statuses: string }
interface State { tickets: Ticket[]; categories: Category[]; jira_config: JiraConfig | null }

const state = ref<State>({ tickets: [], categories: [], jira_config: null });
const page = ref(window.location.hash.slice(1) || "tickets");
const categoryFilter = ref<number | null>(null);
const notice = ref<{ severity: "success" | "error"; text: string } | null>(null);
const busy = ref(false);
const newTicket = ref({ summary: "", description: "", planned_date: null as Date | null, category_id: null as number | null, jira_reference: "" });
const newSubtasks = ref<Array<{ summary: string; description: string; planned_date: Date | null }>>([]);
const newCategory = ref("");
const settings = ref({ base_url: "", browser_base_url: "", email: "", api_token: "", project_key: "", issue_type: "Task", completed_statuses: "Done", validate: false });

const visibleTickets = computed(() => categoryFilter.value === null ? state.value.tickets : state.value.tickets.filter((ticket) => ticket.category_id === categoryFilter.value));
const dueTickets = computed(() => visibleTickets.value.filter((ticket) => !ticket.local_completed && ticket.planned_date && ticket.planned_date <= dateValue(todayDate())));

function go(next: string) { page.value = next; window.location.hash = next; notice.value = null; }
function todayDate() {
  const today = new Date();
  return new Date(today.getFullYear(), today.getMonth(), today.getDate());
}
function dateValue(value: Date | null) {
  return value
    ? `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`
    : "";
}
async function request(url: string, init: RequestInit = {}) {
  const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...(init.headers || {}) } });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.message || "Request failed.");
  if (result.state) state.value = result.state;
  return result;
}
async function load() { const loaded = await (await fetch("/api/state")).json() as State; state.value = loaded; if (loaded.jira_config) Object.assign(settings.value, loaded.jira_config); }
async function run(action: () => Promise<void>, success = "Saved.") {
  busy.value = true; notice.value = null;
  try { await action(); notice.value = { severity: "success", text: success }; }
  catch (error) { notice.value = { severity: "error", text: error instanceof Error ? error.message : "Request failed." }; }
  finally { busy.value = false; }
}
async function createTicket() {
  await run(async () => {
    const result = await request("/api/tickets", { method: "POST", body: JSON.stringify({ ...newTicket.value, planned_date: dateValue(newTicket.value.planned_date) || null }) });
    if (result.created_id) for (const subtask of newSubtasks.value.filter((item) => item.summary.trim())) await request(`/api/tickets/${result.created_id}/subtasks`, { method: "POST", body: JSON.stringify({ ...subtask, planned_date: dateValue(subtask.planned_date) || null }) });
    newTicket.value = { summary: "", description: "", planned_date: null, category_id: null, jira_reference: "" }; newSubtasks.value = [];
    go("tickets"); await load();
  }, "Ticket created.");
}
async function saveTicket(ticket: Ticket) { await run(() => request(`/api/tickets/${ticket.id}`, { method: "PUT", body: JSON.stringify({ summary: ticket.summary, description: ticket.description, planned_date: ticket.planned_date }) }), "Ticket updated."); }
async function saveSubtask(subtask: Ticket) { await run(() => request(`/api/subtasks/${subtask.id}`, { method: "PUT", body: JSON.stringify({ summary: subtask.summary, description: subtask.description, planned_date: subtask.planned_date }) }), "Subtask updated."); }
async function addSubtask(ticket: Ticket, draft: { summary: string; description: string; planned_date: string }) { await run(async () => { await request(`/api/tickets/${ticket.id}/subtasks`, { method: "POST", body: JSON.stringify({ ...draft, planned_date: draft.planned_date || null }) }); }, "Subtask added."); }
async function toggle(url: string) { await run(() => request(url, { method: "POST" }), "Updated."); }
async function remove(url: string) { if (!confirm("Delete this item?")) return; await run(() => request(url, { method: "DELETE" }), "Deleted."); }
async function saveCategory() { await run(async () => { await request("/api/categories", { method: "POST", body: JSON.stringify({ name: newCategory.value }) }); newCategory.value = ""; }, "Category saved."); }
async function deleteCategory(id: number) { if (!confirm("Delete this category? Tickets become uncategorized.")) return; await run(() => request(`/api/categories/${id}`, { method: "DELETE" }), "Category deleted."); }
async function saveSettings() { await run(async () => { const result = await request("/api/settings/jira", { method: "PUT", body: JSON.stringify(settings.value) }); settings.value.api_token = ""; if (result.state.jira_config) Object.assign(settings.value, result.state.jira_config); }, "Jira settings saved."); }
async function sync(url: string) { await run(() => request(url, { method: "POST" }), "Jira sync complete."); }
function addDraftSubtask() { newSubtasks.value.push({ summary: "", description: "", planned_date: null }); }
function removeDraftSubtask(index: number) { if (!confirm("Delete this subtask?")) return; newSubtasks.value.splice(index, 1); }
function categoryName(id: number | null) { return state.value.categories.find((category) => category.id === id)?.name || "Uncategorized"; }
onMounted(() => { load(); window.addEventListener("hashchange", () => { page.value = window.location.hash.slice(1) || "tickets"; }); });
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div><span class="eyebrow">PERSONAL WORKFLOW</span><h1>Work tickets</h1><p>Keep momentum across the work that matters.</p></div>
      <nav aria-label="Main navigation">
        <Button label="Tickets" icon="pi pi-list" :severity="page === 'tickets' ? undefined : 'secondary'" text @click="go('tickets')" />
        <Button label="Create" icon="pi pi-plus" :severity="page === 'create' ? undefined : 'secondary'" text @click="go('create')" />
        <Button label="Categories" icon="pi pi-tags" :severity="page === 'categories' ? undefined : 'secondary'" text @click="go('categories')" />
        <Button label="Settings" icon="pi pi-cog" :severity="page === 'settings' ? undefined : 'secondary'" text @click="go('settings')" />
      </nav>
    </header>
    <Message v-if="notice" :severity="notice.severity" closable @close="notice = null">{{ notice.text }}</Message>

    <main v-if="page === 'tickets'">
      <section class="hero"><div><span class="eyebrow">OVERVIEW</span><h2>Ticket command center</h2><p>See what needs attention today, then shape the queue around your priorities.</p></div><Button label="New ticket" icon="pi pi-plus" @click="go('create')" /></section>
      <Card class="filter-card"><template #content><div class="filter-row"><label for="category-filter">Filter by category</label><Select id="category-filter" v-model="categoryFilter" :options="[{ id: null, name: 'All categories' }, ...state.categories]" optionLabel="name" optionValue="id" placeholder="All categories" /><span>{{ visibleTickets.length }} tickets</span></div></template></Card>
       <section class="ticket-section"><div class="section-heading"><div><span class="eyebrow">FOCUS</span><h2>Today</h2></div><span class="muted">Due today and overdue</span></div><div v-if="dueTickets.length" class="ticket-grid"><TicketCard v-for="ticket in dueTickets" :key="`due-${ticket.id}`" :ticket="ticket" :browser-base-url="state.jira_config?.browser_base_url || ''" :category-name="categoryName(ticket.category_id)" @toggle="toggle(`/api/tickets/${ticket.id}/complete`)" @sync="sync(`/api/tickets/${ticket.id}/sync`)" @remove="remove(`/api/tickets/${ticket.id}`)" @save="saveTicket" @save-subtask="saveSubtask" @add-subtask="addSubtask" @toggle-subtask="toggle(`/api/subtasks/${$event}/complete`)" @remove-subtask="remove(`/api/subtasks/${$event}`)" /></div><div v-else class="empty-state"><i class="pi pi-check-circle"></i><strong>Nothing due right now</strong><span>Add the next useful thing when it arrives.</span></div></section>
       <section class="ticket-section"><div class="section-heading"><div><span class="eyebrow">QUEUE</span><h2>All tickets</h2></div><span class="muted">Drag-and-drop priority coming from the existing API</span></div><div v-if="visibleTickets.length" class="ticket-grid"><TicketCard v-for="ticket in visibleTickets" :key="ticket.id" :ticket="ticket" :browser-base-url="state.jira_config?.browser_base_url || ''" :category-name="categoryName(ticket.category_id)" @toggle="toggle(`/api/tickets/${ticket.id}/complete`)" @sync="sync(`/api/tickets/${ticket.id}/sync`)" @remove="remove(`/api/tickets/${ticket.id}`)" @save="saveTicket" @save-subtask="saveSubtask" @add-subtask="addSubtask" @toggle-subtask="toggle(`/api/subtasks/${$event}/complete`)" @remove-subtask="remove(`/api/subtasks/${$event}`)" /></div><div v-else class="empty-state"><i class="pi pi-inbox"></i><strong>No tickets yet</strong><span>Create a ticket to start your queue.</span></div></section>
    </main>

     <main v-else-if="page === 'create'" class="narrow"><section class="hero"><div><span class="eyebrow">BUILD</span><h2>Create a ticket</h2><p>Capture the outcome and break it into clear next steps.</p></div></section><Card><template #content><form class="form" @submit.prevent="createTicket"><label>Summary or Jira reference<InputText v-model="newTicket.summary" required placeholder="What needs doing? Or WORK-123" /></label><div class="two-col"><label>Planned date<div class="date-control"><DatePicker v-model="newTicket.planned_date" dateFormat="yy-mm-dd" showIcon aria-label="Planned date" /><Button type="button" label="Today" text aria-label="Set planned date to today" @click="newTicket.planned_date = todayDate()" /><Button type="button" label="Unfocus" text aria-label="Remove planned date" :disabled="!newTicket.planned_date" @click="newTicket.planned_date = null" /></div></label><label>Category<Select v-model="newTicket.category_id" :options="state.categories" optionLabel="name" optionValue="id" placeholder="No category" /></label></div><label>Description<Textarea v-model="newTicket.description" rows="5" autoResize /></label><label>Jira import URL (optional)<InputText v-model="newTicket.jira_reference" placeholder="https://your-site/browse/WORK-123" /></label><div class="subtask-builder"><div class="section-heading"><h3>Subtasks</h3><Button type="button" label="Add step" icon="pi pi-plus" text @click="addDraftSubtask" /></div><div v-for="(subtask, index) in newSubtasks" :key="index" class="draft-row"><InputText v-model="subtask.summary" :placeholder="`Step ${index + 1}`" /><div class="date-control"><DatePicker v-model="subtask.planned_date" dateFormat="yy-mm-dd" showIcon :aria-label="`Planned date for step ${index + 1}`" /><Button type="button" label="Today" text :aria-label="`Set planned date for step ${index + 1} to today`" @click="subtask.planned_date = todayDate()" /><Button type="button" label="Unfocus" text :aria-label="`Remove planned date for step ${index + 1}`" :disabled="!subtask.planned_date" @click="subtask.planned_date = null" /></div><Button type="button" icon="pi pi-trash" severity="danger" text @click="removeDraftSubtask(index)" /></div></div><Button type="submit" label="Create ticket" icon="pi pi-check" :loading="busy" /></form></template></Card></main>

    <main v-else-if="page === 'categories'" class="narrow"><section class="hero"><div><span class="eyebrow">ORGANIZE</span><h2>Categories</h2><p>Manage local labels without changing the tickets they organize.</p></div></section><Card><template #content><form class="inline-form" @submit.prevent="saveCategory"><InputText v-model="newCategory" placeholder="e.g. Client work" required /><Button type="submit" label="Add category" icon="pi pi-plus" /></form><div v-if="state.categories.length" class="category-list"><div v-for="category in state.categories" :key="category.id" class="category-item"><span><i class="pi pi-tag"></i>{{ category.name }}</span><Button icon="pi pi-trash" severity="danger" text aria-label="Delete category" @click="deleteCategory(category.id)" /></div></div><div v-else class="empty-state"><i class="pi pi-tags"></i><span>No categories yet.</span></div></template></Card></main>

     <main v-else class="narrow"><section class="hero"><div><span class="eyebrow">CONNECTIONS</span><h2>Application settings</h2><p>Configure the local Jira connection used for sync and imports.</p></div></section><Card><template #content><form class="form" @submit.prevent="saveSettings"><label>Jira API URL<InputText v-model="settings.base_url" type="url" required placeholder="https://api.atlassian.com/..." /></label><label>Jira browser URL<InputText v-model="settings.browser_base_url" type="url" placeholder="https://your-company.atlassian.net" /></label><div class="two-col"><label>Account email<InputText v-model="settings.email" type="email" required /></label><label>Project key<InputText v-model="settings.project_key" required /></label></div><div class="two-col"><label>Issue type<InputText v-model="settings.issue_type" required /></label><label>Completed statuses<InputText v-model="settings.completed_statuses" /></label></div><label>API token<InputText v-model="settings.api_token" type="password" placeholder="Leave blank to keep saved token" /></label><div class="button-row"><Button type="submit" label="Save settings" icon="pi pi-save" :loading="busy" /><Button type="button" label="Save & test connection" severity="secondary" @click="settings.validate = true; saveSettings(); settings.validate = false" /></div></form></template></Card></main>
  </div>
</template>
