-- Enforce tenant RLS even for the table owner/application role.
ALTER TABLE research_history.episodes FORCE ROW LEVEL SECURITY;
