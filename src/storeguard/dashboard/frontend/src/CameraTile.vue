<script setup>
defineProps({
  session: { type: Object, required: true },
  frameUrl: { type: String, default: "" },
  expanded: { type: Boolean, default: false },
});
const emit = defineEmits(["toggle-expand", "close"]);
</script>

<template>
  <figure
    class="tile"
    :class="{
      error: Boolean(session.error),
      stopped: !session.running && !session.error,
      'is-expanded': expanded,
    }"
    @click="emit('toggle-expand')"
  >
    <img :src="frameUrl" :alt="`Detection stream — ${session.filename}`" />
    <figcaption class="tile-hud">
      <span class="tile-name">{{ session.filename }}</span>
      <span class="tile-meta">
        {{
          session.error
            ? session.error
            : `${session.people || 0} ${session.people === 1 ? "person" : "people"} · ${(
                session.fps || 0
              ).toFixed(1)} fps`
        }}
      </span>
    </figcaption>
    <button
      type="button"
      class="tile-close"
      title="Disconnect this camera"
      @click.stop="emit('close')"
    >
      ×
    </button>
  </figure>
</template>
