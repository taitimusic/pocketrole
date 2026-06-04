const { publishedBaseUrl } = window.POCKETROLE_ARCHIVE_INDEX;
const i18n = window.PocketRoleI18n;
const archiveCatalog = document.getElementById('archive-catalog');

async function loadCatalog() {
  if (i18n) {
    i18n.applyStaticLabels();
    i18n.bindLocaleSwitcher();
  }
  const response = await fetch(`${publishedBaseUrl}/catalog.json`, { cache: 'no-store' });
  if (!response.ok) {
    archiveCatalog.textContent = i18n ? i18n.t('archive.empty') : '公開ストーリーはまだありません。';
    return;
  }
  const payload = await response.json();
  archiveCatalog.innerHTML = '';
  payload.stories.forEach((story) => {
    const item = document.createElement('article');
    const link = document.createElement('a');
    const locale = i18n ? i18n.locale : 'ja';
    link.href = `archive.php?story_id=${encodeURIComponent(story.story_id)}&page=1&lang=${encodeURIComponent(locale)}`;
    link.textContent = `${story.title} (${story.story_id})`;
    item.appendChild(link);
    archiveCatalog.appendChild(item);
  });
}

void loadCatalog();
