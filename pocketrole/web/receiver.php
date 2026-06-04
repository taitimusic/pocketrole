<?php
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/chatlog_lib.php';

function receiver_debug_log(string $stage, array $context = []): void {
    $line = [
        'ts' => date('c'),
        'stage' => $stage,
        'context' => $context,
    ];
    $encoded = json_encode($line, JSON_UNESCAPED_UNICODE) . PHP_EOL;
    $debug_path = __DIR__ . '/config/receiver_debug.log';

    if (@error_log($encoded, 3, $debug_path) === false) {
        error_log($encoded);
    }
}

function upsert_day_logs(string $filename, array $logs): void {
    $existing = read_jsonl_file($filename);
    $merged = array_merge($existing, $logs);
    $deduped = dedupe_entries_latest_wins($merged);
    write_jsonl_file($filename, $deduped);
}

function delete_story_dat_logs(string $dir): int {
    ensure_directory_with_index_html($dir, DATA_DIR);
    $files = glob($dir . '/*.dat');
    if ($files === false) {
        return 0;
    }

    $deleted = 0;
    foreach ($files as $file) {
        if (!is_file($file)) {
            continue;
        }
        if (!unlink($file)) {
            throw new RuntimeException('failed to delete log file');
        }
        $deleted++;
    }
    return $deleted;
}

// POST のみ受け付け
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    json_response(['status' => 'error', 'message' => 'Method Not Allowed'], 405);
}

// JSON body 解析
$body = file_get_contents('php://input');
$input = json_decode($body, true);
if (!is_array($input)) {
    json_response(['status' => 'error', 'message' => 'Invalid JSON'], 400);
}
receiver_debug_log('request_start', [
    'method' => $_SERVER['REQUEST_METHOD'] ?? '',
    'body_length' => strlen($body),
]);

// 認証
try {
    $auth_token = load_auth_token();
} catch (RuntimeException $_e) {
    json_response(['status' => 'error', 'message' => 'Auth token config missing'], 500);
}

if (($input['auth_token'] ?? '') !== $auth_token) {
    json_response(['status' => 'error', 'message' => 'Unauthorized'], 401);
}
receiver_debug_log('auth_ok');

// story_id バリデーション
$story_id = preg_replace('/[^a-z0-9_]/', '', (string)($input['story_id'] ?? ''));
if ($story_id === '') {
    json_response(['status' => 'error', 'message' => 'Invalid story_id'], 400);
}
receiver_debug_log('story_id_ok', ['story_id' => $story_id]);

$action = (string)($input['action'] ?? '');
$dir = DATA_DIR . '/' . $story_id;

if ($action === 'reset_story') {
    if (($input['confirm'] ?? false) !== true) {
        json_response(['status' => 'error', 'message' => 'confirm must be true'], 400);
    }

    set_error_handler(function (int $severity, string $message, string $file, int $line): void {
        throw new ErrorException($message, 0, $severity, $file, $line);
    });

    try {
        receiver_debug_log('reset_start', ['dir' => $dir]);
        $deleted = delete_story_dat_logs($dir);
        receiver_debug_log('reset_ok', ['story_id' => $story_id, 'deleted' => $deleted]);
    } catch (Throwable $e) {
        receiver_debug_log('reset_failed', [
            'story_id' => $story_id,
            'dir' => $dir,
            'exception_class' => get_class($e),
            'exception_message' => $e->getMessage(),
        ]);
        restore_error_handler();
        json_response(['status' => 'error', 'message' => 'Receiver reset failed'], 500);
    }

    restore_error_handler();
    json_response(['status' => 'ok', 'action' => 'reset_story', 'deleted' => $deleted]);
}

// logs バリデーション
$logs = $input['logs'] ?? null;
if (!is_array($logs) || count($logs) === 0) {
    json_response(['status' => 'error', 'message' => 'logs is empty or missing'], 400);
}
receiver_debug_log('logs_ok', ['log_count' => count($logs)]);

// ディレクトリ確保
$filename = $dir . '/' . date('Ymd') . '.dat';

set_error_handler(function (int $severity, string $message, string $file, int $line): void {
    throw new ErrorException($message, 0, $severity, $file, $line);
});

try {
    receiver_debug_log('ensure_dir_start', ['dir' => $dir]);
    ensure_directory_with_index_html($dir, DATA_DIR);
    receiver_debug_log('ensure_dir_ok', ['dir' => $dir]);

    receiver_debug_log('upsert_start', [
        'filename' => $filename,
        'log_count' => count($logs),
    ]);
    upsert_day_logs($filename, $logs);
    receiver_debug_log('upsert_ok', [
        'filename' => $filename,
        'log_count' => count($logs),
    ]);
} catch (Throwable $e) {
    receiver_debug_log('write_failed', [
        'story_id' => $story_id,
        'dir' => $dir,
        'filename' => $filename,
        'exception_class' => get_class($e),
        'exception_message' => $e->getMessage(),
    ]);
    restore_error_handler();
    json_response(['status' => 'error', 'message' => 'Receiver write failed'], 500);
}

restore_error_handler();
receiver_debug_log('response_ok', ['story_id' => $story_id, 'log_count' => count($logs)]);

json_response(['status' => 'ok', 'count' => count($logs)]);
