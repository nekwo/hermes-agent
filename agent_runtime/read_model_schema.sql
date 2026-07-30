CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projection_watermarks(
  projection TEXT PRIMARY KEY,
  event_offset INTEGER NOT NULL,
  last_event_ts TEXT, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_instances(instance_id TEXT PRIMARY KEY,
  persona_id TEXT, status TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS operator_channels(channel_id TEXT PRIMARY KEY,
  persona_id TEXT, session_id TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS projections_misc(
  projection TEXT PRIMARY KEY, payload JSON NOT NULL);
