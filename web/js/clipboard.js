export async function copyText(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Loopback browsers may deny async clipboard permission; use the DOM fallback below.
    }
  }
  const previousFocus = document.activeElement;
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute('aria-hidden', 'true');
  Object.assign(textarea.style, { position: 'fixed', left: '-9999px', top: '0', opacity: '0' });
  (previousFocus?.closest('dialog[open]') || document.body).append(textarea);
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand('copy');
  textarea.remove();
  if (previousFocus && previousFocus.isConnected) previousFocus.focus({ preventScroll: true });
  if (!copied) throw new Error('浏览器未允许复制，请手动选择书目信息。');
}
