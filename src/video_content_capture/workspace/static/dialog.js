"use strict";

// Modal dialog helper: focus moves only on explicit user open/close, never on data updates.
// Native modal dialogs close on Esc via the "cancel" event; Tab is kept inside the dialog;
// focus returns to the control that opened it.
(() => {
  const focusable = "button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
    "textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";
  const openers = new WeakMap();

  function trap(event) {
    if (event.key !== "Tab") return;
    const dialog = event.currentTarget;
    const items = [...dialog.querySelectorAll(focusable)].filter((item) => !item.hidden);
    if (!items.length) { event.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function restore(event) {
    const dialog = event.currentTarget;
    const opener = openers.get(dialog);
    openers.delete(dialog);
    const visible = opener && opener.isConnected && opener.getClientRects().length > 0;
    if (visible && !opener.disabled) opener.focus();
    else document.getElementById("workspace")?.focus();
  }

  function open(dialog, opener, initial) {
    if (dialog.open) return;
    openers.set(dialog, opener || document.activeElement);
    if (!dialog.dataset.enhanced) {
      dialog.dataset.enhanced = "true";
      dialog.addEventListener("keydown", trap);
      dialog.addEventListener("close", restore);
      // Esc on a modal dialog fires "cancel" and closes it, except while an action is running.
      dialog.addEventListener("cancel", (event) => {
        if (dialog.dataset.busy === "true") event.preventDefault();
      });
    }
    dialog.showModal();
    (initial || dialog.querySelector(focusable))?.focus();
  }

  function close(dialog) {
    if (dialog.open) dialog.close();
  }

  window.WorkspaceDialog = { open, close };
})();
