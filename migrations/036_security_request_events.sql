BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS security_request_events (
 id bigserial PRIMARY KEY,
 request_id uuid NOT NULL UNIQUE,
 occurred_at timestamptz NOT NULL DEFAULT now(),
 ip text NOT NULL, user_id varchar(128),
 country varchar(2), region varchar(80),
 route varchar(160) NOT NULL, method varchar(10) NOT NULL,
 status integer NOT NULL, duration_ms integer NOT NULL,
 signal varchar(40) NOT NULL, weight integer NOT NULL CHECK(weight BETWEEN 0 AND 10),
 app_version varchar(32), ray_id varchar(80),
 connection_ip text NOT NULL, source varchar(16) NOT NULL CHECK(source IN ('gateway','portal'))
);
CREATE INDEX IF NOT EXISTS idx_security_event_ip_time ON security_request_events(ip,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_event_uid_time ON security_request_events(user_id,occurred_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_security_event_time ON security_request_events(occurred_at);
INSERT INTO schema_migrations(version) VALUES ('036_security_request_events') ON CONFLICT DO NOTHING;
COMMIT;
