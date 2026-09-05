import { createRouter, createWebHistory } from "vue-router";
import DashboardPage from "./pages/DashboardPage.vue";
import CamerasPage from "./pages/CamerasPage.vue";
import TeamPage from "./pages/TeamPage.vue";
import DevicesPage from "./pages/DevicesPage.vue";
import TelegramPage from "./pages/TelegramPage.vue";
import LimeCrmPage from "./pages/LimeCrmPage.vue";
import EventsPage from "./pages/EventsPage.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/dashboard" },
    { path: "/dashboard", name: "dashboard", component: DashboardPage },
    { path: "/cameras", name: "cameras", component: CamerasPage },
    { path: "/team", name: "team", component: TeamPage },
    { path: "/devices", name: "devices", component: DevicesPage },
    { path: "/telegram", name: "telegram", component: TelegramPage },
    { path: "/lime-crm", name: "lime-crm", component: LimeCrmPage },
    { path: "/events", name: "events", component: EventsPage },
  ],
});
