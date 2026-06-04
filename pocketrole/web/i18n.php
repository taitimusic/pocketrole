<?php
// web/i18n.php — public web locale resolver and shell labels

const POCKETROLE_SUPPORTED_LOCALES = ['ja', 'en', 'zh-TW'];
const POCKETROLE_ZH_TW_ALIASES = ['zh', 'zh-TW', 'zh-Hant', 'zh-HK', 'zh-MO'];

function pocketrole_normalize_locale(string $raw_locale): string {
    $locale = trim(str_replace('_', '-', $raw_locale));
    if ($locale === '') {
        return '';
    }
    $lower = strtolower($locale);
    if ($lower === 'ja' || strpos($lower, 'ja-') === 0) {
        return 'ja';
    }
    if ($lower === 'en' || strpos($lower, 'en-') === 0) {
        return 'en';
    }
    if (
        $lower === 'zh'
        || $lower === 'zh-tw'
        || $lower === 'zh-hant'
        || strpos($lower, 'zh-hant-') === 0
        || $lower === 'zh-hk'
        || $lower === 'zh-mo'
    ) {
        return 'zh-TW';
    }
    return '';
}

function pocketrole_resolve_locale(?string $requested_locale = null, ?string $accept_language = null): string {
    $candidates = [];
    if ($requested_locale !== null) {
        $candidates[] = $requested_locale;
    }
    if ($accept_language !== null) {
        foreach (explode(',', $accept_language) as $part) {
            $pieces = explode(';', $part, 2);
            $candidates[] = trim($pieces[0]);
        }
    }

    foreach ($candidates as $candidate) {
        $normalized = pocketrole_normalize_locale((string)$candidate);
        if (in_array($normalized, POCKETROLE_SUPPORTED_LOCALES, true)) {
            return $normalized;
        }
    }
    return 'ja';
}

function pocketrole_locale_query(string $locale): string {
    return rawurlencode(pocketrole_resolve_locale($locale, null));
}

function pocketrole_t(string $locale, string $key): string {
    static $messages = [
        'ja' => [
            'viewer.mapReplay' => 'マップ再生を見る',
            'viewer.timelineBack' => 'タイムラインへ戻る',
            'viewer.storySwitcher' => 'ストーリー切り替え',
            'viewer.connecting' => '接続中...',
            'viewer.jumpLatest' => '最新へ',
            'viewer.jumpLatestAria' => '最新メッセージへ移動',
            'map.titleSuffix' => 'マップ再生',
            'map.subtitle' => '保存済みログを、移動と会話の流れに沿ってシネマ風に再生します。',
            'map.play' => '再生',
            'map.next' => '1件送り',
            'map.reset' => '先頭へ戻る',
            'map.speed' => '速度',
            'map.volume' => '音量',
            'map.stageAria' => 'マップ再生ステージ',
            'map.speaker' => '話者',
            'map.place' => '場所',
            'archive.indexTitle' => '公開ストーリー一覧',
            'archive.storySuffix' => 'の物語',
        ],
        'en' => [
            'viewer.mapReplay' => 'Map Replay',
            'viewer.timelineBack' => 'Back to Timeline',
            'viewer.storySwitcher' => 'Switch story',
            'viewer.connecting' => 'Connecting...',
            'viewer.jumpLatest' => 'Latest',
            'viewer.jumpLatestAria' => 'Jump to latest message',
            'map.titleSuffix' => 'Map Replay',
            'map.subtitle' => 'Replay saved logs as a cinematic flow of movement and dialogue.',
            'map.play' => 'Play',
            'map.next' => 'Next',
            'map.reset' => 'Reset',
            'map.speed' => 'Speed',
            'map.volume' => 'Volume',
            'map.stageAria' => 'Map replay stage',
            'map.speaker' => 'Speaker',
            'map.place' => 'Place',
            'archive.indexTitle' => 'Public Stories',
            'archive.storySuffix' => 'Story',
        ],
        'zh-TW' => [
            'viewer.mapReplay' => '地圖重播',
            'viewer.timelineBack' => '返回時間軸',
            'viewer.storySwitcher' => '切換故事',
            'viewer.connecting' => '連線中...',
            'viewer.jumpLatest' => '最新',
            'viewer.jumpLatestAria' => '移至最新訊息',
            'map.titleSuffix' => '地圖重播',
            'map.subtitle' => '沿著移動與對話的流向，以電影感重播已儲存的紀錄。',
            'map.play' => '播放',
            'map.next' => '下一筆',
            'map.reset' => '回到開頭',
            'map.speed' => '速度',
            'map.volume' => '音量',
            'map.stageAria' => '地圖重播舞台',
            'map.speaker' => '說話者',
            'map.place' => '地點',
            'archive.indexTitle' => '公開故事列表',
            'archive.storySuffix' => '的故事',
        ],
    ];

    $resolved = pocketrole_resolve_locale($locale, null);
    return $messages[$resolved][$key] ?? $messages['ja'][$key] ?? $key;
}
?>
