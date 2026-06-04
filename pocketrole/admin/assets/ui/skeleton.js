/* PocketRole Admin – Skeleton Loading Helpers */
(function () {
  /**
   * buildSkeleton(items)
   *   items: Array of { type: 'title'|'text'|'card', width?: string }
   *   Returns a div.editor-skeleton element.
   */
  window.buildSkeleton = function (items) {
    const wrap = document.createElement('div');
    wrap.className = 'editor-skeleton';
    (items || []).forEach(function (item) {
      const d = document.createElement('div');
      d.className = 'skeleton skeleton-' + (item.type || 'text');
      if (item.width) d.style.maxWidth = item.width;
      wrap.appendChild(d);
    });
    return wrap;
  };
})();
