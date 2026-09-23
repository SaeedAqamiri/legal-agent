-- Research history: durable, tenant-isolated store of published research
-- response payloads so the workspace can be reopened across sessions/restarts.

CREATE SCHEMA IF NOT EXISTS research_history;

CREATE TABLE research_history.episodes (
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    episode_id text NOT NULL CHECK (btrim(episode_id) <> ''),
    conversation_id text NOT NULL CHECK (btrim(conversation_id) <> ''),
    user_id text NOT NULL CHECK (btrim(user_id) <> ''),
    question text NOT NULL CHECK (btrim(question) <> ''),
    applicable_time date NOT NULL,
    created_at timestamptz NOT NULL,
    completed boolean NOT NULL,
    iterations integer NOT NULL CHECK (iterations >= 0),
    has_answer boolean NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (organization_id, episode_id)
);

CREATE INDEX ix_research_history_org_created
    ON research_history.episodes (organization_id, created_at DESC, episode_id);

ALTER TABLE research_history.episodes ENABLE ROW LEVEL SECURITY;

CREATE POLICY research_history_tenant_isolation
    ON research_history.episodes
    USING (organization_id = current_setting('legal_agent.organization_id', true))
    WITH CHECK (organization_id = current_setting('legal_agent.organization_id', true));
