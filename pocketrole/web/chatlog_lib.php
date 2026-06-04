<?php
// web/chatlog_lib.php — chatlog JSONL の正規化・重複排除ヘルパー

require_once __DIR__ . '/config.php';

function path_is_within_root(string $real_dir, string $real_root): bool {
    if ($real_dir === $real_root) {
        return true;
    }

    $prefix = $real_root . DIRECTORY_SEPARATOR;
    return strncmp($real_dir, $prefix, strlen($prefix)) === 0;
}

function ensure_directory_with_index_html(string $dir, ?string $root_dir = null): void {
    $normalized_dir = rtrim($dir, DIRECTORY_SEPARATOR);
    $normalized_root = $root_dir !== null
        ? rtrim($root_dir, DIRECTORY_SEPARATOR)
        : DATA_DIR;

    if (!is_dir($normalized_dir)) {
        mkdir($normalized_dir, 0755, true);
    }
    if (!is_dir($normalized_root)) {
        mkdir($normalized_root, 0755, true);
    }

    $real_dir = realpath($normalized_dir);
    $real_root = realpath($normalized_root);
    if ($real_dir === false || $real_root === false) {
        throw new RuntimeException('Failed to resolve directory for index.html placement');
    }
    if (!path_is_within_root($real_dir, $real_root)) {
        throw new RuntimeException('Directory is outside placeholder root');
    }

    ensure_index_html_file($real_root);

    $relative = substr($real_dir, strlen($real_root));
    $relative = ltrim($relative, DIRECTORY_SEPARATOR);
    if ($relative === '') {
        return;
    }

    $current = $real_root;
    foreach (explode(DIRECTORY_SEPARATOR, $relative) as $part) {
        $current .= DIRECTORY_SEPARATOR . $part;
        ensure_index_html_file($current);
    }
}

function ensure_index_html_file(string $dir): void {
    $index_path = $dir . DIRECTORY_SEPARATOR . 'index.html';
    if (!file_exists($index_path)) {
        touch($index_path);
    }
}

function read_jsonl_file(string $path): array {
    if (!file_exists($path)) {
        return [];
    }
    $rows = [];
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $decoded = json_decode($line, true);
        if (is_array($decoded)) {
            $rows[] = $decoded;
        }
    }
    return $rows;
}

function write_jsonl_file(string $path, array $rows): void {
    $dir = dirname($path);
    ensure_directory_with_index_html($dir, DATA_DIR);

    $tmp = tempnam($dir, basename($path) . '.');
    if ($tmp === false) {
        throw new RuntimeException('Failed to create temporary chatlog file');
    }

    $body = '';
    foreach ($rows as $row) {
        $body .= json_encode($row, JSON_UNESCAPED_UNICODE) . "\n";
    }

    file_put_contents($tmp, $body, LOCK_EX);
    rename($tmp, $path);
}

function parse_sim_datetime(string $raw): ?DateTimeImmutable {
    $text = trim($raw);
    if ($text === '') {
        return null;
    }

    $formats = [
        'Y-m-d\TH:i:s',
        'Y-m-d\TH:i',
        'Y-m-d H:i:s',
        'Y-m-d H:i',
    ];

    foreach ($formats as $format) {
        $dt = DateTimeImmutable::createFromFormat($format, $text);
        $errors = DateTimeImmutable::getLastErrors();
        $has_errors = $errors !== false
            && ($errors['warning_count'] > 0 || $errors['error_count'] > 0);
        if ($dt !== false && !$has_errors) {
            return $dt;
        }
    }

    try {
        return new DateTimeImmutable($text);
    } catch (Exception $e) {
        return null;
    }
}

function normalize_sim_datetime(string $raw): string {
    $dt = parse_sim_datetime($raw);
    if ($dt === null) {
        return trim($raw);
    }
    return $dt->format('Y-m-d\TH:i');
}

function make_logical_key(array $entry): string {
    $sim = normalize_sim_datetime((string)($entry['sim_datetime'] ?? ''));
    $char = (string)($entry['char_id'] ?? '');
    $turn = (string)($entry['turn_number'] ?? '');
    return $sim . '|' . $char . '|' . $turn;
}

function compare_log_entries(array $a, array $b): int {
    $a_norm = normalize_sim_datetime((string)($a['sim_datetime'] ?? ''));
    $b_norm = normalize_sim_datetime((string)($b['sim_datetime'] ?? ''));
    $a_dt = parse_sim_datetime($a_norm);
    $b_dt = parse_sim_datetime($b_norm);

    if ($a_dt !== null && $b_dt !== null) {
        if ($a_dt < $b_dt) {
            return -1;
        }
        if ($a_dt > $b_dt) {
            return 1;
        }
    } else {
        $cmp = strcmp($a_norm, $b_norm);
        if ($cmp !== 0) {
            return $cmp;
        }
    }

    $a_turn = (int)($a['turn_number'] ?? 0);
    $b_turn = (int)($b['turn_number'] ?? 0);
    if ($a_turn !== $b_turn) {
        return $a_turn <=> $b_turn;
    }

    $a_char = (string)($a['char_id'] ?? '');
    $b_char = (string)($b['char_id'] ?? '');
    $cmp = strcmp($a_char, $b_char);
    if ($cmp !== 0) {
        return $cmp;
    }

    return strcmp((string)($a['message'] ?? ''), (string)($b['message'] ?? ''));
}

function dedupe_entries_latest_wins(array $entries): array {
    $latest = [];
    foreach ($entries as $entry) {
        $latest[make_logical_key($entry)] = $entry;
    }
    $result = array_values($latest);
    usort($result, 'compare_log_entries');
    return $result;
}

function collect_latest_entries(string $story_dir, int $limit): array {
    $files = glob($story_dir . '/*.dat');
    if ($files === false) {
        $files = [];
    }
    rsort($files, SORT_STRING);

    $latest = [];
    $has_more = false;

    foreach ($files as $file) {
        $entries = read_jsonl_file($file);
        for ($i = count($entries) - 1; $i >= 0; $i--) {
            $entry = $entries[$i];
            $key = make_logical_key($entry);
            if (isset($latest[$key])) {
                continue;
            }
            if (count($latest) < $limit) {
                $latest[$key] = $entry;
            } else {
                $has_more = true;
                break 2;
            }
        }
    }

    $rows = array_values($latest);
    usort($rows, 'compare_log_entries');
    return [$rows, $has_more];
}

function collect_history_entries(string $story_dir, string $from_str, string $to_str, int $limit): array {
    $files = glob($story_dir . '/*.dat');
    if ($files === false) {
        $files = [];
    }
    sort($files, SORT_STRING);

    $entries = [];
    foreach ($files as $file) {
        $base = basename($file, '.dat');
        if ($base < $from_str || $base > $to_str) {
            continue;
        }
        $entries = array_merge($entries, read_jsonl_file($file));
    }

    $rows = dedupe_entries_latest_wins($entries);
    $has_more = count($rows) > $limit;
    if ($has_more) {
        $rows = array_slice($rows, 0, $limit);
    }
    return [$rows, $has_more];
}
