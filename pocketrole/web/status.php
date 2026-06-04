<?php
require_once __DIR__ . '/config.php';

// GET のみ受け付け（認証不要 — ヘルスチェック用公開エンドポイント）
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    json_response(['status' => 'error', 'message' => 'Method Not Allowed'], 405);
}

// DATA_DIR 以下のサブディレクトリを列挙
$entries = glob(DATA_DIR . '/*', GLOB_ONLYDIR);
if ($entries === false) {
    $entries = [];
}

if (count($entries) === 0) {
    json_response(['status' => 'error', 'message' => 'No stories found'], 404);
}

$stories = [];
foreach ($entries as $dir) {
    $story_id = basename($dir);

    // story_id が [a-z0-9_] のみで構成されているか確認
    if (!preg_match('/^[a-z0-9_]+$/', $story_id)) {
        continue;
    }

    // .dat ファイルを全て取得して総行数と最終更新時刻を集計
    $dat_files = glob($dir . '/*.dat');
    if ($dat_files === false) {
        $dat_files = [];
    }

    $log_count    = 0;
    $latest_mtime = 0;

    foreach ($dat_files as $dat_file) {
        $mtime = filemtime($dat_file);
        if ($mtime !== false && $mtime > $latest_mtime) {
            $latest_mtime = $mtime;
        }

        $content = file($dat_file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        if ($content !== false) {
            $log_count += count($content);
        }
    }

    $last_received = $latest_mtime > 0
        ? date('Y-m-d H:i:s', $latest_mtime)
        : null;

    $stories[] = [
        'story_id'      => $story_id,
        'last_received' => $last_received,
        'log_count'     => $log_count,
    ];
}

if (count($stories) === 0) {
    json_response(['status' => 'error', 'message' => 'No valid stories found'], 404);
}

json_response([
    'status'        => 'ok',
    'stories'       => $stories,
    'total_stories' => count($stories),
    'server_time'   => date('Y-m-d H:i:s'),
]);
