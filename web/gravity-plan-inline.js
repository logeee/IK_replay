import "/web/gravity-plan-viewer.js?v=20260822-10";

let loadedPlanId = "";

async function syncCurrentPlan() {
  if (document.hidden) return;
  try {
    const response = await fetch("/api/gravity/status", { cache: "no-store" });
    const payload = await response.json();
    const planId = payload?.plan?.id;
    if (
      !response.ok
      || payload?.ok === false
      || !planId
      || planId === loadedPlanId
    ) return;
    loadedPlanId = planId;
    window.dispatchEvent(new CustomEvent("gravity:preview-plan", {
      detail: { planId },
    }));
  } catch (_error) {
    // The next poll retries; service availability is shown by the parent page.
  }
}

syncCurrentPlan();
setInterval(syncCurrentPlan, 1500);
document.addEventListener("visibilitychange", syncCurrentPlan);
