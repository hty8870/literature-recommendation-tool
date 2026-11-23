// 极小的 DOM 构建工具。所有不可信文本一律通过 textContent 写入，绝不拼接 HTML。

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'open' || key === 'hidden' || key === 'disabled') {
      node[key] = Boolean(value);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false || child === '') continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function setHidden(node, hidden) {
  if (!node || node.hidden === hidden) return;
  node.hidden = hidden;
}

export function setText(node, text) {
  const value = String(text ?? '');
  if (node.textContent !== value) node.textContent = value;
}

export function reconcileChildren(parent, children) {
  const desired = new Set(children);
  for (const child of [...parent.childNodes]) {
    if (!desired.has(child)) child.remove();
  }
  let cursor = parent.firstChild;
  for (const child of children) {
    if (child !== cursor) {
      if (parent.isConnected && child.isConnected && typeof parent.moveBefore === 'function') {
        parent.moveBefore(child, cursor);
      } else {
        const focus = document.activeElement;
        const selection = document.getSelection();
        const range = selection?.rangeCount && child.contains(selection.anchorNode) && child.contains(selection.focusNode)
          ? [selection.anchorNode, selection.anchorOffset, selection.focusNode, selection.focusOffset] : null;
        parent.insertBefore(child, cursor);
        if (child.contains(focus) && focus.isConnected && document.activeElement !== focus) {
          focus.focus({ preventScroll: true });
        }
        if (range) selection.setBaseAndExtent(...range);
      }
    }
    cursor = child.nextSibling;
  }
}

export function mono(value, title = null) {
  return el('code', { text: value == null || value === '' ? '—' : String(value), title: title || undefined });
}
