<script setup lang="ts">
import { computed } from "vue";
import Select from "primevue/select";
import type { Category, CategoryComponent } from "./CategoryButtons.vue";

interface ComponentOption extends CategoryComponent {
  value: string;
  disabled?: boolean;
}

const props = defineProps<{
  categories: Category[];
  components: CategoryComponent[];
  categoryId: number | null;
}>();

const selectedComponent = defineModel<string | null>({ required: true });

const options = computed<ComponentOption[]>(() => {
  const category = props.categories.find((candidate) => candidate.id === props.categoryId);
  const ordered = [...(category?.components || []), ...props.components];
  const seen = new Set<number>();
  const active: ComponentOption[] = ordered.filter((component) => {
    if (seen.has(component.id)) return false;
    seen.add(component.id);
    return true;
  }).map((component) => ({ ...component, value: component.name }));
  if (selectedComponent.value && !props.components.some((component) => component.name === selectedComponent.value)) {
    active.unshift({
      id: -1,
      name: `${selectedComponent.value} (deleted)`,
      value: selectedComponent.value,
      disabled: true,
    });
  }
  return active;
});
</script>

<template>
  <Select
    v-model="selectedComponent"
    :options="options"
    optionLabel="name"
    optionValue="value"
    optionDisabled="disabled"
    placeholder="No component"
    showClear
    filter
    aria-label="Ticket component"
  />
</template>
