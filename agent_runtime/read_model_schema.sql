CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projection_watermarks(
  projection TEXT PRIMARY KEY,
  event_offset INTEGER NOT NULL,
  last_event_ts TEXT, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goals(
  id TEXT PRIMARY KEY, state TEXT NOT NULL, title TEXT,
  workspace_id TEXT, realm_id TEXT, updated_at TEXT,
  payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS stage_verification(
  goal_id TEXT NOT NULL, stage_id TEXT NOT NULL,
  owner TEXT, observed_status TEXT, observed_proof_count INTEGER,
  authoritative_status TEXT, authoritative_proof_count INTEGER,
  tamper_flag INTEGER NOT NULL DEFAULT 0, payload JSON NOT NULL,
  PRIMARY KEY(goal_id, stage_id));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, task_id TEXT, persona_id TEXT,
  state TEXT, stage_id TEXT, updated_at TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS proofs(id TEXT PRIMARY KEY, task_id TEXT, stage_id TEXT,
  type TEXT, status TEXT, created_by TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, task_id TEXT, kind TEXT,
  state TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS agent_instances(instance_id TEXT PRIMARY KEY,
  persona_id TEXT, status TEXT, task_id TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS operator_channels(channel_id TEXT PRIMARY KEY,
  persona_id TEXT, session_id TEXT, payload JSON NOT NULL);
CREATE TABLE IF NOT EXISTS projections_misc(
  projection TEXT PRIMARY KEY, payload JSON NOT NULL);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_proofs_task_stage ON proofs(task_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_incidents_task ON incidents(task_id, state);
