// The CV page starts a company sync and then navigates away; the preferences page needs
// to know which run to follow. Session storage is a per-tab convenience, never state the
// app depends on: it can be unavailable or empty, and everything must read correctly
// without it.
const KEY = "companySyncRunId";

export function rememberSyncRun(runId: number): void {
  try {
    window.sessionStorage.setItem(KEY, String(runId));
  } catch {
    // Storage unavailable: the summary falls back to the stored company list.
  }
}

export function recallSyncRun(): number | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    const id = raw === null ? NaN : Number(raw);
    return Number.isInteger(id) && id > 0 ? id : null;
  } catch {
    return null;
  }
}
