(() => {
  const STORAGE_KEY = 'citation.ui-theme.v1';
  const themes = [
    { choice: '1', id: 'paper', label: '简洁经典' },
    { choice: '2', id: 'caas', label: '农科院门户' },
    { choice: '3', id: 'editorial', label: '学术札记' },
    { choice: '4', id: 'lab', label: '研究工作台' },
  ];
  const choices = new Map(themes.flatMap((theme) => [[theme.choice, theme], [theme.id, theme]]));
  const url = new URL(window.location.href);
  const requested = choices.get(url.searchParams.get('theme'));
  let remembered = null;
  try { remembered = window.localStorage.getItem(STORAGE_KEY); } catch { /* use this page only */ }
  const active = requested || choices.get(remembered) || themes[0];
  const root = document.documentElement;
  root.dataset.uiTheme = active.id;
  root.dataset.uiThemeChoice = active.choice;
  root.style.colorScheme = active.id === 'lab' ? 'dark' : 'light';

  const scheme = document.querySelector('meta[name="color-scheme"]');
  if (scheme) scheme.content = active.id === 'lab' ? 'dark' : 'light';
  const browserColor = document.querySelector('meta[name="theme-color"]');
  if (browserColor) {
    browserColor.content = { paper: '#f7f8f5', editorial: '#f4efe5', lab: '#10161b', caas: '#075ca8' }[active.id];
  }
  if (active.id !== 'paper') {
    const link = document.createElement('link');
    link.id = 'theme-overrides';
    link.rel = 'stylesheet';
    link.href = new URL(`themes/${active.id}.css`, document.baseURI).href;
    document.head.append(link);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const mount = document.getElementById('theme-switch');
    if (!mount) return;
    const label = document.createElement('span');
    label.className = 'theme-switch__label';
    label.textContent = '界面';
    const select = document.createElement('select');
    select.id = 'theme-picker';
    select.className = 'theme-picker';
    select.setAttribute('aria-label', '选择界面风格');
    for (const theme of themes) {
      const option = document.createElement('option');
      option.value = theme.choice;
      option.textContent = `${theme.choice} ${theme.label}`;
      select.append(option);
    }
    select.value = active.choice;
    select.addEventListener('change', () => {
      const next = choices.get(select.value);
      if (!next || next.choice !== select.value || next.id === active.id) return;
      try { window.localStorage.setItem(STORAGE_KEY, next.choice); } catch { /* keep the explicit URL choice */ }
      const destination = new URL(window.location.href);
      destination.searchParams.set('theme', next.choice);
      window.location.replace(destination.href);
    });
    mount.replaceChildren(label, select);
  }, { once: true });
})();
