import { el } from './dom.js';
import { failureMessage } from './chat-model.js';

export class BibliographyMenu {
  constructor({ load, copy, onToast }) {
    this.load = load;
    this.copy = copy;
    this.onToast = onToast;
    this.trigger = null;
    this.node = el('div', { class: 'bibliography-menu', role: 'menu', id: 'bibliography-menu',
      'aria-label': '选择引用格式', hidden: true });
    this.node.addEventListener('keydown', (event) => this.onKey(event));
    document.addEventListener('pointerdown', (event) => {
      if (!this.node.contains(event.target) && !this.trigger?.contains(event.target)) this.close();
    });
    window.addEventListener('resize', () => this.position());
    window.addEventListener('scroll', (event) => {
      if (event.target !== this.node) this.position();
    }, true);
    document.addEventListener('close', (event) => {
      if (event.target === this.node.parentElement) this.close();
    }, true);
  }

  open(trigger, target, { raw = '', fallback = '' } = {}) {
    if (this.trigger === trigger) {
      this.close({ focus: true });
      return;
    }
    this.close();
    this.trigger = trigger;
    const controller = new AbortController();
    this.request = controller;
    (trigger.closest('dialog[open]') || document.body).append(this.node);
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-controls', this.node.id);
    trigger.setAttribute('aria-expanded', 'true');
    this.node.hidden = false;
    const note = el('p', { class: 'bibliography-menu__note', role: 'status',
      text: target ? '正在读取书目格式…' : '模拟条目仅提供原始题录。' });
    const choices = el('div', { role: 'group' });
    this.node.replaceChildren(note, choices);
    const basic = raw || fallback;
    if (basic) this.appendOption(choices, { label: raw ? '原始题录（未核查）' : '已有规范书目', text: basic });
    if (target) this.node.setAttribute('aria-busy', 'true');
    this.position();
    if (basic) this.focusItem(0);
    if (target) void this.loadOptions(target, choices, note, { raw, fallback }, controller);
  }

  async loadOptions(target, choices, note, { raw, fallback }, controller) {
    try {
      const payload = await this.load(target, { signal: controller.signal });
      if (controller.signal.aborted || !this.trigger) return;
      const formats = Array.isArray(payload?.formats) ? payload.formats : [];
      const active = this.node.contains(document.activeElement) || document.activeElement === this.trigger;
      choices.replaceChildren();
      for (const option of formats) this.appendOption(choices, option);
      const available = formats.some((option) => typeof option.text === 'string' && option.text.trim());
      if (raw) this.appendOption(choices, { label: '原始题录（未核查）', text: raw });
      else if (!available && fallback) this.appendOption(choices, { label: '已有规范书目', text: fallback });
      if (available) note.remove();
      else note.textContent = '缺少可核对的题录字段，不能生成所选引用格式。';
      if (active) this.focusItem(0);
    } catch (error) {
      if (controller.signal.aborted || !this.trigger) return;
      note.textContent = failureMessage(error, '规范书目暂不可用。');
    } finally {
      if (this.request === controller) {
        this.node.removeAttribute('aria-busy');
        this.position();
      }
    }
  }

  appendOption(choices, option) {
    const text = typeof option?.text === 'string' ? option.text : '';
    const label = option?.label || '未命名格式';
    const item = el('button', { type: 'button', role: 'menuitem', tabindex: -1,
      'aria-disabled': String(!text), text: text ? label : `${label}（不可用）`,
      title: !text ? option?.failure?.message : undefined });
    if (text) item.addEventListener('click', () => {
      void this.copyOption(text, label);
    });
    choices.append(item);
  }

  async copyOption(text, label) {
    try {
      await this.copy(text);
      this.close({ focus: true });
      this.onToast(`已复制：${label}`);
    } catch (error) {
      this.onToast(failureMessage(error, '复制失败，请检查浏览器剪贴板权限。'), { level: 'error' });
    }
  }

  focusItem(index) {
    const items = [...this.node.querySelectorAll('[role="menuitem"]')];
    if (items.length) items[(index + items.length) % items.length].focus({ preventScroll: true });
  }

  onKey(event) {
    const items = [...this.node.querySelectorAll('[role="menuitem"]')];
    const index = items.indexOf(document.activeElement);
    if (event.key === 'Tab') {
      this.close({ focus: true });
      return;
    }
    if (!['Escape', 'ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'Escape') this.close({ focus: true });
    else if (event.key === 'Home') this.focusItem(0);
    else if (event.key === 'End') this.focusItem(items.length - 1);
    else this.focusItem(event.key === 'ArrowDown' ? index + 1 : index - 1);
  }

  position() {
    const trigger = this.trigger;
    if (!trigger || this.node.hidden) return;
    if (!trigger.isConnected || !trigger.getClientRects().length) {
      this.close();
      return;
    }
    const bounds = trigger.getBoundingClientRect();
    const margin = 8;
    const right = document.documentElement.clientWidth;
    const bottom = document.documentElement.clientHeight;
    const pane = this.trigger.closest('.results-panel');
    const clip = pane && getComputedStyle(pane).overflowY === 'auto' ? pane.getBoundingClientRect() : null;
    if (bounds.bottom <= Math.max(0, clip?.top ?? 0) || bounds.top >= Math.min(bottom, clip?.bottom ?? bottom)) {
      this.close();
      return;
    }
    const width = this.node.offsetWidth;
    const below = bottom - bounds.bottom - margin * 2;
    const above = bounds.top - margin * 2;
    const height = this.node.scrollHeight + 2;
    const openAbove = height > below && above > below;
    const maxHeight = Math.max(0, openAbove ? above : below);
    Object.assign(this.node.style, {
      left: `${Math.max(margin, Math.min(bounds.right - width, right - width - margin))}px`,
      top: `${openAbove ? Math.max(margin, bounds.top - margin - Math.min(height, maxHeight)) : bounds.bottom + margin}px`,
      maxHeight: `${maxHeight}px`,
    });
  }

  close({ focus = false } = {}) {
    this.request?.abort();
    this.request = null;
    const trigger = this.trigger;
    this.trigger = null;
    this.node.hidden = true;
    this.node.removeAttribute('aria-busy');
    this.node.remove();
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
    if (focus && trigger?.isConnected) trigger.focus({ preventScroll: true });
  }
}
