<?php
// web/viewer.php — タイムラインビューアー（公開ページ）

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/i18n.php';

// GETのみ許可
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    header('Allow: GET');
    exit;
}

// story_id バリデーション
$raw_story_id = $_GET['story_id'] ?? '';
$story_id = preg_replace('/[^a-z0-9_]/', '', strtolower($raw_story_id));

if ($story_id === '') {
    http_response_code(400);
    echo 'story_id is required';
    exit;
}

$locale = pocketrole_resolve_locale($_GET['lang'] ?? null, $_SERVER['HTTP_ACCEPT_LANGUAGE'] ?? null);

// ディレクトリ存在確認
$story_dir = DATA_DIR . '/' . $story_id;
if (!is_dir($story_dir)) {
    http_response_code(404);
    echo 'story not found: ' . htmlspecialchars($story_id, ENT_QUOTES);
    exit;
}

function asset_url($path) {
    $full_path = __DIR__ . '/' . $path;
    if (!is_file($full_path)) {
        return $path;
    }
    return $path . '?v=' . rawurlencode((string) filemtime($full_path));
}
?>
<!DOCTYPE html>
<html lang="<?= htmlspecialchars($locale, ENT_QUOTES) ?>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars($story_id, ENT_QUOTES) ?> — PocketRole</title>
  <link rel="stylesheet" href="<?= htmlspecialchars(asset_url('assets/style.css'), ENT_QUOTES) ?>">
</head>
<body>
  <header>
    <h1 id="story-title"><?= htmlspecialchars($story_id, ENT_QUOTES) ?></h1>
    <div id="header-right">
      <a class="secondary-link replay-cta" data-i18n="viewer.mapReplay" href="map_replay.php?story_id=<?= urlencode($story_id) ?>&lang=<?= pocketrole_locale_query($locale) ?>">マップ再生を見る</a>
      <select id="locale-switcher" class="locale-switcher" title="Language">
        <option value="ja">日本語</option>
        <option value="en">English</option>
        <option value="zh-TW">繁體中文</option>
      </select>
      <select id="story-switcher" title="ストーリー切り替え" data-i18n-title="viewer.storySwitcher"></select>
      <span id="poll-status" data-i18n="viewer.connecting">接続中...</span>
    </div>
  </header>

  <main>
    <div id="timeline"></div>
    <button id="jump-latest" type="button" aria-label="最新メッセージへ移動" data-i18n="viewer.jumpLatest" data-i18n-aria-label="viewer.jumpLatestAria">最新へ</button>
  </main>

  <script>
    window.POCKETROLE = {
      storyId:   "<?= htmlspecialchars($story_id, ENT_QUOTES) ?>",
      locale:    "<?= htmlspecialchars($locale, ENT_QUOTES) ?>",
      apiUrl:    "api.php",
      statusUrl: "status.php",
      imageBaseUrl: "assets/character_images"
    };
  </script>
  <script src="<?= htmlspecialchars(asset_url('assets/i18n.js'), ENT_QUOTES) ?>"></script>
  <script src="<?= htmlspecialchars(asset_url('assets/app.js'), ENT_QUOTES) ?>"></script>
</body>
</html>
