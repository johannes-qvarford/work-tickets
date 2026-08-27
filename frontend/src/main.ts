import { createApp } from "vue";
import PrimeVue from "primevue/config";
import Aura from "@primevue/themes/aura";
import "primeicons/primeicons.css";
import App from "./App.vue";
import "./style.css";

createApp(App).use(PrimeVue, { theme: { preset: Aura } }).mount("#app");
