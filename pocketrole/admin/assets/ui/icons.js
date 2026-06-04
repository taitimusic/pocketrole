/* PocketRole Admin – SVG Icon Sprite (heroicons-flavored) */
(function () {
  const ICONS = {
    logo:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 2 4 7v6c0 4.5 3.4 8.4 8 9 4.6-.6 8-4.5 8-9V7l-8-5z"/>' +
      '<path d="m9 12 2 2 4-4"/></svg>',
    dashboard:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="3" width="7" height="9" rx="1.5"/>' +
      '<rect x="14" y="3" width="7" height="5" rx="1.5"/>' +
      '<rect x="14" y="12" width="7" height="9" rx="1.5"/>' +
      '<rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    stories:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v15l-4-2-4 2-4-2-4 2V5z"/></svg>',
    settings:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="3"/>' +
      '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    audit:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
      '<polyline points="14 2 14 8 20 8"/>' +
      '<line x1="8" y1="13" x2="16" y2="13"/>' +
      '<line x1="8" y1="17" x2="16" y2="17"/></svg>',
    sun:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="4"/>' +
      '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
    moon:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
    hamburger:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<line x1="3" y1="6" x2="21" y2="6"/>' +
      '<line x1="3" y1="12" x2="21" y2="12"/>' +
      '<line x1="3" y1="18" x2="21" y2="18"/></svg>',
    chevronRight:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<polyline points="9 6 15 12 9 18"/></svg>',
    check:
      '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
      '<path d="M16.7 5.3a1 1 0 0 1 0 1.4l-7 7a1 1 0 0 1-1.4 0l-3-3a1 1 0 1 1 1.4-1.4L9 11.6l6.3-6.3a1 1 0 0 1 1.4 0z"/></svg>',
    alert:
      '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
      '<path d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM9 6a1 1 0 1 1 2 0v4a1 1 0 0 1-2 0V6zm0 8a1 1 0 1 1 2 0 1 1 0 0 1-2 0z"/></svg>',
    warning:
      '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
      '<path d="M8.3 3.1a2 2 0 0 1 3.4 0l6.6 11A2 2 0 0 1 16.6 17H3.4a2 2 0 0 1-1.7-3l6.6-10.9zM10 7a1 1 0 0 0-1 1v3a1 1 0 1 0 2 0V8a1 1 0 0 0-1-1zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2z"/></svg>',
    info:
      '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
      '<path d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM9 9a1 1 0 0 1 2 0v5a1 1 0 1 1-2 0V9zm0-3a1 1 0 1 1 2 0 1 1 0 0 1-2 0z"/></svg>',
    close:
      '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<line x1="5" y1="5" x2="15" y2="15"/>' +
      '<line x1="15" y1="5" x2="5" y2="15"/></svg>'
  };

  window.ADMIN_ICON = ICONS;

  window.iconElement = function (name) {
    const span = document.createElement('span');
    span.className = 'admin-icon';
    span.innerHTML = ICONS[name] || '';
    return span;
  };
})();
