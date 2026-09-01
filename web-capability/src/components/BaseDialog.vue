<script setup lang="ts">
defineProps<{ title: string; wide?: boolean }>();
const emit = defineEmits<{ close: [] }>();
</script>

<template>
  <Teleport to="body">
    <div class="overlay" @mousedown.self="emit('close')">
      <div class="dialog" :class="{ wide }">
        <header>
          <h3>{{ title }}</h3>
          <button class="x" @click="emit('close')">✕</button>
        </header>
        <div class="body">
          <slot />
        </div>
        <footer>
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(4, 8, 16, 0.66);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  padding: 24px;
}

.dialog {
  width: min(540px, 94vw);
  max-height: 88vh;
  overflow: auto;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.55);
  animation: pop 0.18s ease;
}

.dialog.wide {
  width: min(640px, 94vw);
}

@keyframes pop {
  from {
    opacity: 0;
    transform: translateY(10px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px 0;
}

h3 {
  margin: 0;
  font-size: 16px;
}

.x {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 15px;
  padding: 4px 8px;
  border-radius: 7px;
}

.x:hover {
  color: var(--text);
  background: var(--bg-soft);
}

.body {
  padding: 16px 22px 4px;
}

footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 22px 20px;
}
</style>
