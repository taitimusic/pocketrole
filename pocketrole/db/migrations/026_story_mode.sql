-- ============================================================
-- Migration 026: story_mode (ジャンル・トーン別 quality/style 設定)
-- ============================================================

-- stories テーブルに story_mode カラムを追加する。
-- 既存の stories には既定値 'drama' が設定される。
-- 有効値: drama / comedy / romance / action

ALTER TABLE stories ADD COLUMN story_mode TEXT NOT NULL DEFAULT 'drama';
