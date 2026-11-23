// Heroicons outline (MIT): https://github.com/tailwindlabs/heroicons
const PATHS = {
  'adjustments-horizontal': 'M3 6h5m4 0h9M3 12h11m4 0h3M3 18h2m4 0h12M10 4v4m6 2v4M7 16v4',
  'arrow-left': 'M10.5 19.5 3 12m0 0 7.5-7.5M3 12h18',
  'arrow-path': 'M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99',
  'arrow-right': 'M13.5 4.5 21 12m0 0-7.5 7.5M21 12H3',
  'arrow-top-right-on-square': 'M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25',
  'arrow-up-tray': 'M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5',
  'book-open': 'M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292m0-14.25v14.25',
  bookmark: 'M17.593 3.322c1.1.128 1.907 1.077 1.907 2.185V21L12 17.25 4.5 21V5.507c0-1.108.806-2.057 1.907-2.185a48.507 48.507 0 0 1 11.186 0Z',
  check: 'm4.5 12.75 6 6 9-13.5',
  'chevron-down': 'm19.5 8.25-7.5 7.5-7.5-7.5',
  clipboard: 'M9 12h6m-6 3h6m-3-9h.008v.008H12V6Zm0 3h.008v.008H12V9Zm-6.75 9.75h13.5A2.25 2.25 0 0 0 21 16.5v-9A2.25 2.25 0 0 0 18.75 5.25h-2.5a3.75 3.75 0 0 0-7.5 0h-2.5A2.25 2.25 0 0 0 3 7.5v9a2.25 2.25 0 0 0 2.25 2.25Z',
  'exclamation-circle': 'M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z',
  'paper-airplane': 'M6 12 3.269 3.125A59.769 59.769 0 0 1 21.485 12 59.768 59.768 0 0 1 3.27 20.875L5.999 12Zm0 0h7.5',
  'user-circle': 'M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1',
  'ellipsis': 'M5 12h.01M12 12h.01M19 12h.01',
  plus: 'M12 4.5v15m7.5-7.5h-15',
  stop: 'M5.25 7.5A2.25 2.25 0 0 1 7.5 5.25h9a2.25 2.25 0 0 1 2.25 2.25v9a2.25 2.25 0 0 1-2.25 2.25h-9a2.25 2.25 0 0 1-2.25-2.25v-9Z',
  'x-mark': 'M6 18 18 6M6 6l12 12',
};

function paintIcon(svg, name) {
  const pathData = PATHS[name] || PATHS['exclamation-circle'];
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.6');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', `icon ${svg.getAttribute('class') || ''}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', pathData);
  svg.replaceChildren(path);
}

export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  paintIcon(svg, name);
  return svg;
}

export function hydrateIcons(root = document) {
  for (const svg of root.querySelectorAll('svg[data-icon]')) paintIcon(svg, svg.dataset.icon);
}
