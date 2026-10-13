export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // HTTP origins commonly block the Clipboard API; fall back to a selected textarea.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    const copied = document.execCommand?.("copy") ?? false;
    if (!copied) throw new Error("Clipboard access is unavailable");
  } finally {
    const restoreFocus = document.activeElement === textarea;
    textarea.remove();
    if (restoreFocus && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  }
}
