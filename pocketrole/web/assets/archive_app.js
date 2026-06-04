const { storyId, publishedBaseUrl } = window.POCKETROLE_ARCHIVE;
const i18n = window.PocketRoleI18n;

const params = new URLSearchParams(window.location.search);
const currentPage = Number.parseInt(params.get('page') || '1', 10);
const archiveMeta = document.getElementById('archive-meta');
const archiveContent = document.getElementById('archive-content');
const archivePagination = document.getElementById('archive-pagination');

function renderPagination(pageCount, activePage) {
  archivePagination.innerHTML = '';
  const locale = i18n ? i18n.locale : 'ja';
  for (let page = 1; page <= pageCount; page += 1) {
    const link = document.createElement('a');
    link.href = `archive.php?story_id=${encodeURIComponent(storyId)}&page=${page}&lang=${encodeURIComponent(locale)}`;
    link.textContent = String(page);
    if (page === activePage) {
      link.setAttribute('aria-current', 'page');
    }
    archivePagination.appendChild(link);
  }
}

async function loadArchive() {
  if (i18n) {
    i18n.applyStaticLabels();
    i18n.bindLocaleSwitcher();
  }
  const manifestResp = await fetch(`${publishedBaseUrl}/stories/${storyId}/manifest.json`, { cache: 'no-store' });
  if (!manifestResp.ok) {
    archiveMeta.textContent = i18n ? i18n.t('archive.noArchive') : '公開アーカイブがありません。';
    return;
  }
  const manifest = await manifestResp.json();
  const pageResp = await fetch(`${publishedBaseUrl}/stories/${storyId}/pages/${String(currentPage).padStart(4, '0')}.json`, { cache: 'no-store' });
  if (!pageResp.ok) {
    archiveMeta.textContent = i18n ? i18n.t('archive.pageLoadFailed') : 'ページを読み込めませんでした。';
    return;
  }
  const pageData = await pageResp.json();
  archiveMeta.textContent = i18n
    ? i18n.t('archive.meta', { title: manifest.title, page: currentPage, pageCount: manifest.page_count })
    : `${manifest.title} / ${currentPage} / ${manifest.page_count}`;
  archiveContent.innerHTML = '';
  pageData.scenes.forEach((scene) => {
    const section = document.createElement('section');
    const heading = document.createElement('h2');
    heading.textContent = scene.title;
    section.appendChild(heading);
    const summary = document.createElement('p');
    summary.textContent = scene.summary;
    section.appendChild(summary);
    scene.outputs.forEach((output) => {
      const paragraph = document.createElement('p');
      paragraph.textContent = output.content;
      section.appendChild(paragraph);
    });
    archiveContent.appendChild(section);
  });
  renderPagination(manifest.page_count, currentPage);
}

void loadArchive();
