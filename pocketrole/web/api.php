<?php
require_once __DIR__ . '/config.php';
require_once __DIR__ . '/chatlog_lib.php';
// chatlog_lib.php には normalize_sim_datetime(), make_logical_key(), dedupe_entries_latest_wins() がある

const NARRATOR_CHAR_ID = '_narrator';
const NARRATOR_DISPLAY_NAME = 'ナレーション';
const NARRATOR_AVATAR_URL = 'assets/character_images/_system/narration/neutral.png';

function load_story_metadata(string $story_id): array {
    $path = __DIR__ . '/assets/story_metadata/' . $story_id . '.json';
    if (!is_file($path)) {
        return [];
    }

    $decoded = json_decode((string)file_get_contents($path), true);
    if (!is_array($decoded)) {
        return [];
    }

    return $decoded;
}

function build_char_name_map(array $metadata): array {
    $characters = $metadata['characters'] ?? [];
    $map = [];
    foreach ($characters as $character) {
        if (!is_array($character)) {
            continue;
        }
        $char_id = (string)($character['id'] ?? '');
        $char_name = (string)($character['name'] ?? '');
        if ($char_id !== '' && $char_name !== '') {
            $map[$char_id] = $char_name;
        }
    }
    return $map;
}

function enrich_log_entry(array $entry, array $name_map): array {
    $char_id = (string)($entry['char_id'] ?? '');
    if ($char_id === NARRATOR_CHAR_ID) {
        $entry['char_name'] = NARRATOR_DISPLAY_NAME;
        return $entry;
    }
    if ($char_id !== '' && isset($name_map[$char_id])) {
        $entry['char_name'] = $name_map[$char_id];
    }
    return $entry;
}

function build_avatar_url(string $story_id, string $char_id): ?string {
    if ($char_id === '') {
        return null;
    }
    if ($char_id === NARRATOR_CHAR_ID) {
        $absolute_path = __DIR__ . '/' . NARRATOR_AVATAR_URL;
        return is_file($absolute_path) ? NARRATOR_AVATAR_URL : null;
    }

    $relative_path = 'assets/character_images/' . $story_id . '/' . $char_id . '/neutral.png';
    $absolute_path = __DIR__ . '/' . $relative_path;
    return is_file($absolute_path) ? $relative_path : null;
}

function normalize_replay_character(string $story_id, string $char_id, string $char_name): array {
    if ($char_id === NARRATOR_CHAR_ID) {
        return [
            'char_id'    => NARRATOR_CHAR_ID,
            'char_name'  => NARRATOR_DISPLAY_NAME,
            'avatar_url' => build_avatar_url($story_id, NARRATOR_CHAR_ID),
        ];
    }

    return [
        'char_id'    => $char_id,
        'char_name'  => $char_name !== '' ? $char_name : $char_id,
        'avatar_url' => build_avatar_url($story_id, $char_id),
    ];
}

function build_replay_payload(string $story_id, array $story_metadata, array $entries, array $char_name_map): array {
    $story_title = (string)($story_metadata['title'] ?? $story_id);

    $places = [];
    $seen_places = [];
    $characters = [];
    $seen_characters = [];
    $events = [];

    foreach ($entries as $index => $entry) {
        $char_id = (string)($entry['char_id'] ?? '');
        $char_name = (string)($entry['char_name'] ?? ($char_name_map[$char_id] ?? $char_id));
        $character = normalize_replay_character($story_id, $char_id, $char_name);
        $place_id = isset($entry['place_id']) ? (string)$entry['place_id'] : null;

        if ($place_id !== null && $place_id !== '' && !isset($seen_places[$place_id])) {
            $seen_places[$place_id] = true;
            $places[] = [
                'place_id' => $place_id,
                'label'    => $place_id,
            ];
        }

        if ($char_id !== '' && !isset($seen_characters[$char_id])) {
            $seen_characters[$char_id] = true;
            $characters[] = $character;
        }

        $events[] = [
            'seq'              => $index + 1,
            'sim_datetime'     => $entry['sim_datetime'] ?? null,
            'char_id'          => $char_id,
            'char_name'        => $character['char_name'],
            'place_id'         => $place_id !== '' ? $place_id : null,
            'message'          => $entry['message'] ?? null,
            'expression'       => $entry['expression'] ?? null,
            'emotion_snapshot' => $entry['emotion_snapshot'] ?? null,
        ];
    }

    return [
        'status'     => 'ok',
        'story_id'   => $story_id,
        'action'     => 'replay',
        'story'      => ['title' => $story_title],
        'places'     => $places,
        'characters' => $characters,
        'events'     => $events,
        'count'      => count($events),
    ];
}

// GET のみ受け付け
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    json_response(['status' => 'error', 'message' => 'Method Not Allowed'], 405);
}

// story_id サニタイズ
$story_id = preg_replace('/[^a-z0-9_]/', '', (string)($_GET['story_id'] ?? ''));
if ($story_id === '') {
    json_response(['status' => 'error', 'message' => 'Invalid story_id'], 400);
}

// ディレクトリ存在確認
$story_dir = DATA_DIR . '/' . $story_id;
if (!is_dir($story_dir)) {
    json_response(['status' => 'error', 'message' => 'story_id not found'], 404);
}

$story_metadata = load_story_metadata($story_id);
$char_name_map = build_char_name_map($story_metadata);

// action 確認
$action = $_GET['action'] ?? '';
if (!in_array($action, ['latest', 'history', 'characters', 'replay'], true)) {
    json_response(['status' => 'error', 'message' => 'Invalid action'], 400);
}

// latest / replay 以外は認証必須
if ($action !== 'latest' && $action !== 'replay') {
    try {
        $auth_token = load_auth_token();
    } catch (RuntimeException $_e) {
        json_response(['status' => 'error', 'message' => 'Auth token config missing'], 500);
    }

    if (($_GET['auth_token'] ?? '') !== $auth_token) {
        json_response(['status' => 'error', 'message' => 'Unauthorized'], 401);
    }
}

// ===========================================================
// action=latest
// ===========================================================
if ($action === 'latest') {
    $limit = min((int)($_GET['limit'] ?? 20), 100);
    [$collected, $has_more] = collect_latest_entries($story_dir, $limit);
    $collected = array_map(
        function (array $entry) use ($char_name_map): array {
            return enrich_log_entry($entry, $char_name_map);
        },
        $collected
    );

    json_response([
        'status'   => 'ok',
        'story_id' => $story_id,
        'action'   => 'latest',
        'logs'     => $collected,
        'count'    => count($collected),
        'has_more' => $has_more,
    ]);
}

// ===========================================================
// action=replay
// ===========================================================
if ($action === 'replay') {
    [$collected, $_has_more] = collect_latest_entries($story_dir, 300);
    $collected = array_map(
        function (array $entry) use ($char_name_map): array {
            return enrich_log_entry($entry, $char_name_map);
        },
        $collected
    );

    json_response(build_replay_payload($story_id, $story_metadata, $collected, $char_name_map));
}

// ===========================================================
// action=history
// ===========================================================
if ($action === 'history') {
    $limit = min((int)($_GET['limit'] ?? 100), 100);

    // from / to パース
    $from_raw = $_GET['from'] ?? '';
    $to_raw   = $_GET['to'] ?? '';

    $from_dt = DateTime::createFromFormat('Y-m-d', $from_raw);
    $to_dt   = DateTime::createFromFormat('Y-m-d', $to_raw);
    if ($from_dt === false || $to_dt === false) {
        json_response(['status' => 'error', 'message' => 'Invalid from/to date (expected YYYY-MM-DD)'], 400);
    }

    $from_str = $from_dt->format('Ymd');
    $to_str   = $to_dt->format('Ymd');

    [$collected, $has_more] = collect_history_entries($story_dir, $from_str, $to_str, $limit);
    $collected = array_map(
        function (array $entry) use ($char_name_map): array {
            return enrich_log_entry($entry, $char_name_map);
        },
        $collected
    );

    json_response([
        'status'   => 'ok',
        'story_id' => $story_id,
        'action'   => 'history',
        'logs'     => $collected,
        'count'    => count($collected),
        'has_more' => $has_more,
    ]);
}

// ===========================================================
// action=characters
// ===========================================================
if ($action === 'characters') {
    $files = glob($story_dir . '/*.dat');
    if ($files === false) {
        $files = [];
    }
    rsort($files, SORT_STRING);
    $files = array_slice($files, 0, 2);

    $entries = [];
    foreach (array_reverse($files) as $file) {
        $entries = array_merge($entries, read_jsonl_file($file));
    }
    $entries = dedupe_entries_latest_wins($entries);

    $char_latest = [];
    foreach ($entries as $entry) {
        $char_id = (string)($entry['char_id'] ?? '');
        if ($char_id !== '') {
            $char_latest[$char_id] = $entry;
        }
    }

    $characters = [];
    foreach ($char_latest as $char_id => $entry) {
        $characters[] = [
            'char_id'          => $char_id,
            'char_name'        => $char_name_map[$char_id] ?? $char_id,
            'current_place'    => $entry['place_id'] ?? null,
            'last_message'     => $entry['message'] ?? null,
            'last_update'      => $entry['sim_datetime'] ?? null,
            'current_emotion'  => $entry['emotion_snapshot'] ?? null,
            'current_expression' => $entry['expression'] ?? null,
        ];
    }

    json_response([
        'status'     => 'ok',
        'story_id'   => $story_id,
        'action'     => 'characters',
        'characters' => $characters,
        'count'      => count($characters),
    ]);
}
