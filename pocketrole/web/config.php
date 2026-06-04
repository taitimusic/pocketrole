<?php
// web/config.php — Web側設定

// チャットログ保存先（receiver.php からの相対パス）
define('DATA_DIR', __DIR__ . '/chatlog');

function auth_token_file_path(): string {
    return __DIR__ . '/config/auth_token.txt';
}

function load_auth_token(): string {
    static $token = null;
    if ($token !== null) {
        return $token;
    }

    $path = auth_token_file_path();
    if (!is_file($path) || !is_readable($path)) {
        throw new RuntimeException('auth token config missing');
    }

    $loaded = trim((string)file_get_contents($path));
    if ($loaded === '') {
        throw new RuntimeException('auth token config empty');
    }

    $token = $loaded;
    return $token;
}

// JSON レスポンス共通ヘルパー
function json_response(array $data, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}
