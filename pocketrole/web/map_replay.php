<?php
// web/map_replay.php — Phaser ベースの公開マップ再生ビューアー

require_once __DIR__ . '/config.php';
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

$story_dir = DATA_DIR . '/' . $story_id;
if (!is_dir($story_dir)) {
    http_response_code(404);
    echo 'story not found: ' . htmlspecialchars($story_id, ENT_QUOTES);
    exit;
}
?>
<!DOCTYPE html>
<html lang="<?= htmlspecialchars($locale, ENT_QUOTES) ?>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?= htmlspecialchars($story_id, ENT_QUOTES) ?> — Map Replay</title>
  <link rel="stylesheet" href="assets/map_replay.css">
</head>
<body>
  <header class="page-header">
    <div>
      <h1><span><?= htmlspecialchars($story_id, ENT_QUOTES) ?></span> <span data-i18n="map.titleSuffix">マップ再生</span></h1>
      <p class="page-subtitle" data-i18n="map.subtitle">保存済みログを、移動と会話の流れに沿ってシネマ風に再生します。</p>
    </div>
    <nav class="page-nav">
      <a href="viewer.php?story_id=<?= urlencode($story_id) ?>&lang=<?= pocketrole_locale_query($locale) ?>" data-i18n="viewer.timelineBack">タイムラインへ戻る</a>
      <select id="locale-switcher" class="locale-switcher" title="Language">
        <option value="ja">日本語</option>
        <option value="en">English</option>
        <option value="zh-TW">繁體中文</option>
      </select>
    </nav>
  </header>

  <main class="page-layout">
    <div class="replay-controls">
      <button id="play-toggle" type="button" data-i18n="map.play">再生</button>
      <button id="next-event" type="button" data-i18n="map.next">1件送り</button>
      <button id="reset-replay" type="button" data-i18n="map.reset">先頭へ戻る</button>
      <button id="bgm-toggle" type="button">BGM ON</button>
      <label for="speed-select" data-i18n="map.speed">速度</label>
      <select id="speed-select">
        <option value="0.5">0.5x</option>
        <option value="1" selected>1x</option>
        <option value="2">2x</option>
        <option value="4">4x</option>
      </select>
      <label for="bgm-volume" data-i18n="map.volume">音量</label>
      <input id="bgm-volume" class="bgm-volume" type="range" min="0" max="1" step="0.05" value="0.55">
    </div>

    <div class="stage-shell">
      <div id="map-stage" aria-label="マップ再生ステージ" data-i18n-aria-label="map.stageAria"></div>
      <div id="unsupported-state" class="unsupported-state" hidden></div>
      <div class="minimal-hud">
        <span id="status-speaker">話者: -</span>
        <span id="status-place">場所: -</span>
        <span id="status-progress">0 / 0</span>
      </div>
    </div>
  </main>

  <script>
    window.POCKETROLE_MAP_REPLAY = {
      storyId: "<?= htmlspecialchars($story_id, ENT_QUOTES) ?>",
      locale: "<?= htmlspecialchars($locale, ENT_QUOTES) ?>",
      apiUrl: "api.php",
      replayPageUrl: "map_replay.php",
      viewerPageUrl: "viewer.php",
      imageBaseUrl: "assets/character_images",
      storyMapBaseUrl: "assets/story_maps",
      defaultBgmUrl: "assets/audio/pocketrole_bgm.mp3"
    };
  </script>
  <script src="assets/phaser.min.js"></script>
  <script src="assets/i18n.js"></script>
  <script src="assets/map_replay.js"></script>
</body>
</html>
