-- Law notebook: organization-scoped learned navigation memory over the legal
-- display tree (docs/qavanin/06_law_notebook.md §6). Event-sourced + a merged
-- page summary. Independent of the deterministic graph: nothing here is ever
-- treated as canonical law; the agent re-reads canonical text for any answer.
--
-- topics: stable topic identity (never key on a Persian folder name)
-- events: append-only learning log per episode
-- routes: research shortcuts (topic -> node) with a confirmed weight
-- pages:  merged per-node/topic summary + freshness markers

CREATE SCHEMA IF NOT EXISTS notebook;

CREATE TABLE notebook.topics (
    topic_id        text PRIMARY KEY CHECK (btrim(topic_id) <> ''),
    name            text NOT NULL CHECK (btrim(name) <> ''),
    parent_topic_id text REFERENCES notebook.topics (topic_id),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notebook.events (
    event_id        text PRIMARY KEY CHECK (btrim(event_id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    episode_id      text,
    topic_id        text REFERENCES notebook.topics (topic_id),
    node_id         text,
    event_type      text NOT NULL CHECK (event_type IN (
                        'route_taken', 'relation_discovered',
                        'answer_confirmed', 'summary_merged')),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_notebook_events_org_created
    ON notebook.events (organization_id, created_at DESC, event_id);

CREATE TABLE notebook.routes (
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    topic_id        text NOT NULL REFERENCES notebook.topics (topic_id),
    to_node_id      text NOT NULL CHECK (btrim(to_node_id) <> ''),
    weight          double precision NOT NULL DEFAULT 0 CHECK (weight >= 0),
    hits            integer NOT NULL DEFAULT 0 CHECK (hits >= 0),
    confirmed_hits  integer NOT NULL DEFAULT 0 CHECK (confirmed_hits >= 0),
    status          text NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate', 'approved', 'rejected')),
    source_episode_id text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, topic_id, to_node_id)
);

CREATE TABLE notebook.pages (
    organization_id   text NOT NULL CHECK (btrim(organization_id) <> ''),
    node_id           text NOT NULL CHECK (btrim(node_id) <> ''),
    kind              text NOT NULL CHECK (kind IN (
                          'root', 'branch', 'domain', 'instrument', 'provision')),
    path              text,
    summary           text NOT NULL DEFAULT '',
    summary_version   integer NOT NULL DEFAULT 0,
    based_on          jsonb NOT NULL DEFAULT '{}'::jsonb,
    needs_revalidation boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, node_id)
);

ALTER TABLE notebook.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebook.events FORCE ROW LEVEL SECURITY;
CREATE POLICY notebook_events_tenant_isolation ON notebook.events
    USING (organization_id = current_setting('legal_agent.organization_id', true))
    WITH CHECK (organization_id = current_setting('legal_agent.organization_id', true));

ALTER TABLE notebook.routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebook.routes FORCE ROW LEVEL SECURITY;
CREATE POLICY notebook_routes_tenant_isolation ON notebook.routes
    USING (organization_id = current_setting('legal_agent.organization_id', true))
    WITH CHECK (organization_id = current_setting('legal_agent.organization_id', true));

ALTER TABLE notebook.pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebook.pages FORCE ROW LEVEL SECURITY;
CREATE POLICY notebook_pages_tenant_isolation ON notebook.pages
    USING (organization_id = current_setting('legal_agent.organization_id', true))
    WITH CHECK (organization_id = current_setting('legal_agent.organization_id', true));
