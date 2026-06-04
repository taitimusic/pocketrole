BEGIN;

ALTER TABLE stories ADD COLUMN web_receiver_url TEXT;
ALTER TABLE stories ADD COLUMN web_auth_token   TEXT;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (34, 'per-story web post target url and auth token');

COMMIT;
