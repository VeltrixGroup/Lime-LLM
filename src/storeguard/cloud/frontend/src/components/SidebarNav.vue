<script setup>
import { isOwner } from "../store";
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand">storeguard</div>
    <nav class="nav" aria-label="Main">
      <router-link class="nav-link" active-class="active" to="/dashboard">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="12" width="4" height="8" rx="1"/><rect x="10" y="7" width="4" height="13" rx="1"/><rect x="16" y="3" width="4" height="17" rx="1"/></svg>
        Dashboard
      </router-link>
      <router-link class="nav-link" active-class="active" to="/cameras">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5c0-.8.6-1.5 1.5-1.5h2l1-1.5h5l1 1.5h2A1.5 1.5 0 0 1 17 8.5v8A1.5 1.5 0 0 1 15.5 18h-11A1.5 1.5 0 0 1 3 16.5z"/><circle cx="10" cy="12" r="3.2"/><path d="M18.5 10.5 21 9v8l-2.5-1.5"/></svg>
        Cameras
      </router-link>
      <!-- Plain anchor, not router-link: /live/ is a separate app (the
           storeguard dashboard) proxied through this origin, not a route
           inside this SPA's router. -->
      <a class="nav-link" href="/live/">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/><path d="m10.5 8 4 2.5-4 2.5z" fill="currentColor" stroke="none"/></svg>
        Live view
      </a>
      <router-link class="nav-link" active-class="active" to="/team">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><circle cx="17" cy="7" r="2.4"/><path d="M15.7 14.2c2.4.3 4.3 2.1 4.3 4.8"/></svg>
        Team
      </router-link>
      <router-link v-if="isOwner()" class="nav-link" active-class="active" to="/devices">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v2.3M13 3v2.3M9 18.7V21M13 18.7V21M3 9h2.3M3 13h2.3M18.7 9H21M18.7 13H21"/></svg>
        Edge devices
      </router-link>
      <router-link v-if="isOwner()" class="nav-link" active-class="active" to="/telegram">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3 3.5 10.3c-.9.4-.9 1.7.1 2l4.9 1.6 1.7 5.2c.3.9 1.5 1.1 2.1.3l2.4-3 4.6 3.4c.8.6 1.9.1 2-.9L21 3Z"/><path d="M8.5 13.9 18 6.5"/></svg>
        Telegram
      </router-link>
      <router-link class="nav-link" active-class="active" to="/events">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h3.5l2-6 4 12 2-8 1.5 2H21"/></svg>
        Events
      </router-link>
    </nav>
  </aside>
</template>
