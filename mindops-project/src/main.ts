import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import { initRevealEngine } from "@/lib/reveal-engine";
import "./styles.css";

initRevealEngine();

createApp(App).use(router).mount("#app");
