import { createRouter, createWebHistory } from "vue-router";
import OverviewPage from "./pages/OverviewPage.vue";
import CamerasPage from "./pages/CamerasPage.vue";
import IncidentsPage from "./pages/IncidentsPage.vue";
import EmployeesPage from "./pages/EmployeesPage.vue";
import AnalyticsPage from "./pages/AnalyticsPage.vue";
import AccessPage from "./pages/AccessPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";

// `perm`: hidden from the sidebar (and redirected away) without it.
export const NAV = [
  { path: "/overview", label: "Обзор", icon: "overview" },
  { path: "/cameras", label: "Камеры онлайн", icon: "camera", perm: "live" },
  { path: "/incidents", label: "Инциденты", icon: "incident", perm: "archive" },
  { path: "/employees", label: "Сотрудники", icon: "user", perm: "staff_stats" },
  { path: "/analytics", label: "Аналитика", icon: "chart", perm: "analytics" },
  { path: "/access", label: "Доступ и роли", icon: "shield", perm: "users" },
  { path: "/settings", label: "Настройки", icon: "sliders", perm: "settings" },
];

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: OverviewPage, meta: { title: "Обзор" } },
    { path: "/cameras", component: CamerasPage, meta: { perm: "live", title: "Камеры онлайн" } },
    { path: "/incidents", component: IncidentsPage, meta: { perm: "archive", title: "Инциденты" } },
    { path: "/employees", component: EmployeesPage, meta: { perm: "staff_stats", title: "Сотрудники" } },
    { path: "/analytics", component: AnalyticsPage, meta: { perm: "analytics", title: "Аналитика" } },
    { path: "/access", component: AccessPage, meta: { perm: "users", title: "Доступ и роли" } },
    { path: "/settings", component: SettingsPage, meta: { perm: "settings", title: "Настройки" } },
    // Pages of the previous cabinet.
    { path: "/dashboard", redirect: "/overview" },
    { path: "/events", redirect: "/incidents" },
    { path: "/team", redirect: "/access" },
    { path: "/devices", redirect: "/settings" },
    { path: "/telegram", redirect: "/settings" },
    { path: "/lime-crm", redirect: "/settings" },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

router.afterEach((to) => {
  document.title = `${to.meta.title || "Панель"} · Лайм AI`;
});
