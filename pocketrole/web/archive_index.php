<?php
// web/archive_index.php — 公開 archive 一覧

require_once __DIR__ . '/i18n.php';

if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    header('Allow: GET');
    exit;
}

$locale = pocketrole_resolve_locale($_GET['lang'] ?? null, $_SERVER['HTTP_ACCEPT_LANGUAGE'] ?? null);
?>
<!DOCTYPE html>
<html lang="<?= htmlspecialchars($locale, ENT_QUOTES) ?>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PocketRole Archive</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <header>
    <h1 data-i18n="archive.indexTitle">公開ストーリー一覧</h1>
    <select id="locale-switcher" class="locale-switcher" title="Language">
      <option value="ja">日本語</option>
      <option value="en">English</option>
      <option value="zh-TW">繁體中文</option>
    </select>
  </header>
  <main>
    <div id="archive-catalog"></div>
  </main>
  <script>
    window.POCKETROLE_ARCHIVE_INDEX = {
      locale: "<?= htmlspecialchars($locale, ENT_QUOTES) ?>",
      publishedBaseUrl: "published"
    };
  </script>
  <script src="assets/i18n.js"></script>
  <script src="assets/archive_index.js"></script>
</body>
</html>
