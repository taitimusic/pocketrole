/* PocketRole Admin – Toast Helper */
(function () {
  const STACK_ID = 'toast-stack';
  const DEFAULT_DURATION = {
    success: 3200,
    info: 3500,
    warning: 5000,
    error: 6000
  };

  function ensureStack() {
    let stack = document.getElementById(STACK_ID);
    if (!stack) {
      stack = document.createElement('div');
      stack.id = STACK_ID;
      stack.setAttribute('aria-live', 'polite');
      stack.setAttribute('aria-atomic', 'false');
      document.body.appendChild(stack);
    }
    return stack;
  }

  function iconHtml(kind) {
    const icons = window.ADMIN_ICON || {};
    if (kind === 'success') return icons.check || '';
    if (kind === 'error')   return icons.alert || '';
    if (kind === 'warning') return icons.warning || '';
    return icons.info || '';
  }

  function removeToast(toast) {
    if (!toast || !toast.parentNode) return;
    toast.classList.add('removing');
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 240);
  }

  /**
   * showToast(message, kind?, options?)
   *   kind: 'success' | 'error' | 'warning' | 'info'
   *   options: { title?, duration? (ms; 0 = sticky) }
   */
  window.showToast = function (message, kind, options) {
    if (!kind) kind = 'info';
    if (!options) options = {};
    const stack = ensureStack();

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + kind;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');

    const icon = document.createElement('div');
    icon.className = 'toast-icon';
    icon.innerHTML = iconHtml(kind);
    toast.appendChild(icon);

    const body = document.createElement('div');
    body.className = 'toast-body';
    if (options.title) {
      const titleEl = document.createElement('div');
      titleEl.className = 'toast-title';
      titleEl.textContent = options.title;
      body.appendChild(titleEl);
      const msgEl = document.createElement('div');
      msgEl.className = 'toast-msg';
      msgEl.textContent = message;
      body.appendChild(msgEl);
    } else {
      const titleEl = document.createElement('div');
      titleEl.className = 'toast-title';
      titleEl.textContent = message;
      body.appendChild(titleEl);
    }
    toast.appendChild(body);

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'toast-close';
    closeBtn.setAttribute('aria-label', '閉じる');
    closeBtn.innerHTML = (window.ADMIN_ICON && window.ADMIN_ICON.close) || '×';
    closeBtn.addEventListener('click', () => removeToast(toast));
    toast.appendChild(closeBtn);

    stack.appendChild(toast);

    const duration = options.duration != null
      ? options.duration
      : (DEFAULT_DURATION[kind] || DEFAULT_DURATION.info);
    if (duration > 0) {
      setTimeout(() => removeToast(toast), duration);
    }
    return toast;
  };

  window.dismissToasts = function () {
    const stack = document.getElementById(STACK_ID);
    if (!stack) return;
    Array.from(stack.children).forEach(removeToast);
  };
})();
