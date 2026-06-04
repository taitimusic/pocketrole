<?php
// web/archive.php — 公開 story archive viewer

require_once __DIR__ . '/i18n.php';

if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    header('Allow: GET');
    exit;
}

$raw_story_id = $_GET['story_id'] ?? '';
$story_id = preg_replace('/[^a-z0-9_]/', '', strtolower($raw_story_id));

if ($story_id === '') {
    http_response_code(400);
    echo 'story_id is required';
    exit;
}

$locale = pocketrole_resolve_locale($_GET['lang'] ?? null, $_SERVER['HTTP_ACCEPT_LANGUAGE'] ?? null);
?>
<!DOCTYPE html>
<html lang="<?= htmlspecialchars($locale, ENT_QUOTES) ?>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars($story_id, ENT_QUOTES) ?> — Archive</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <header>
    <h1><span><?= htmlspecialchars($story_id, ENT_QUOTES) ?></span> <span data-i18n="archive.storySuffix">の物語</span></h1>
    <nav>
      <a href="archive_index.php?lang=<?= pocketrole_locale_query($locale) ?>" data-i18n="archive.indexTitle">公開ストーリー一覧</a>
      <select id="locale-switcher" class="locale-switcher" title="Language">
        <option value="ja">日本語</option>
        <option value="en">English</option>
        <option value="zh-TW">繁體中文</option>
      </select>
    </nav>
  </header>
  <main>
    <div id="archive-meta"></div>
    <div id="archive-content"></div>
    <div id="archive-pagination"></div>
  </main>
  <script>
    window.POCKETROLE_ARCHIVE = {
      storyId: "<?= htmlspecialchars($story_id, ENT_QUOTES) ?>",
      locale: "<?= htmlspecialchars($locale, ENT_QUOTES) ?>",
      publishedBaseUrl: "published"
    };
  </script>
  <script src="assets/i18n.js"></script>
  <script src="assets/archive_app.js"></script>
</body>
</html>
