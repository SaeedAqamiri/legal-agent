"""Postgres adapter for the organization-scoped law notebook."""

from __future__ import annotations

from typing import Any

from ..notebook import NotebookEvent, NotebookPage, NotebookRoute
from ..repositories import NotebookRepository


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


class PostgresNotebookRepository(NotebookRepository):
    """DB-API/psycopg adapter; transaction ownership stays with the caller."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def connect(cls, dsn: str, **kwargs: Any) -> PostgresNotebookRepository:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional package
            raise RuntimeError(
                "install the postgres extra: pip install -e '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn, **kwargs))

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.connection.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    def save_events(self, events: tuple[NotebookEvent, ...]) -> None:
        with self.connection.cursor() as cur:
            for event in events:
                cur.execute(
                    """INSERT INTO notebook.events
                           (event_id, organization_id, episode_id, topic_id, node_id,
                            event_type, payload, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (event_id) DO NOTHING""",
                    (
                        event.event_id,
                        event.organization_id,
                        event.episode_id,
                        event.topic_id,
                        event.node_id,
                        event.event_type.value,
                        _jsonb(event.payload),
                        event.created_at,
                    ),
                )

    def upsert_route(
        self,
        organization_id: str,
        topic_id: str,
        to_node_id: str,
        *,
        hit: bool,
        confirmed: bool,
        source_episode_id: str | None = None,
    ) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO notebook.routes
                       (organization_id, topic_id, to_node_id, weight, hits,
                        confirmed_hits, status, source_episode_id, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'candidate', %s, now())
                   ON CONFLICT (organization_id, topic_id, to_node_id) DO UPDATE
                       SET hits = notebook.routes.hits + EXCLUDED.hits,
                           confirmed_hits = notebook.routes.confirmed_hits + EXCLUDED.confirmed_hits,
                           weight = notebook.routes.weight + EXCLUDED.weight,
                           updated_at = now()""",
                (
                    organization_id,
                    topic_id,
                    to_node_id,
                    (1.0 if confirmed else 0.2) if hit else 0.0,
                    1 if hit else 0,
                    1 if confirmed else 0,
                    source_episode_id,
                ),
            )

    def get_page(self, organization_id: str, node_id: str) -> NotebookPage | None:
        rows = self._rows(
            """SELECT organization_id, node_id, kind, path, summary, summary_version,
                      based_on, needs_revalidation, updated_at
                 FROM notebook.pages
                WHERE organization_id = %s AND node_id = %s""",
            (organization_id, node_id),
        )
        return _page(rows[0]) if rows else None

    def pages(self, organization_id: str, kind: str | None = None) -> tuple[NotebookPage, ...]:
        if kind is None:
            rows = self._rows(
                """SELECT organization_id, node_id, kind, path, summary, summary_version,
                          based_on, needs_revalidation, updated_at
                     FROM notebook.pages WHERE organization_id = %s ORDER BY node_id""",
                (organization_id,),
            )
        else:
            rows = self._rows(
                """SELECT organization_id, node_id, kind, path, summary, summary_version,
                          based_on, needs_revalidation, updated_at
                     FROM notebook.pages WHERE organization_id = %s AND kind = %s
                    ORDER BY node_id""",
                (organization_id, kind),
            )
        return tuple(_page(row) for row in rows)

    def routes(
        self, organization_id: str, topic_id: str | None = None
    ) -> tuple[NotebookRoute, ...]:
        if topic_id is None:
            rows = self._rows(
                """SELECT organization_id, topic_id, to_node_id, weight, hits,
                          confirmed_hits, status, source_episode_id
                     FROM notebook.routes WHERE organization_id = %s
                    ORDER BY confirmed_hits DESC, weight DESC""",
                (organization_id,),
            )
        else:
            rows = self._rows(
                """SELECT organization_id, topic_id, to_node_id, weight, hits,
                          confirmed_hits, status, source_episode_id
                     FROM notebook.routes WHERE organization_id = %s AND topic_id = %s
                    ORDER BY confirmed_hits DESC, weight DESC""",
                (organization_id, topic_id),
            )
        return tuple(
            NotebookRoute(
                organization_id=row[0],
                topic_id=row[1],
                to_node_id=row[2],
                weight=row[3],
                hits=row[4],
                confirmed_hits=row[5],
                status=row[6],
                source_episode_id=row[7],
            )
            for row in rows
        )

    def upsert_page(self, page: NotebookPage) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO notebook.pages
                       (organization_id, node_id, kind, path, summary, summary_version,
                        based_on, needs_revalidation, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (organization_id, node_id) DO UPDATE
                       SET kind = EXCLUDED.kind, path = EXCLUDED.path,
                           summary = EXCLUDED.summary,
                           summary_version = EXCLUDED.summary_version,
                           based_on = EXCLUDED.based_on,
                           needs_revalidation = EXCLUDED.needs_revalidation,
                           updated_at = now()""",
                (
                    page.organization_id,
                    page.node_id,
                    page.kind,
                    page.path,
                    page.summary,
                    page.summary_version,
                    _jsonb(page.based_on),
                    page.needs_revalidation,
                ),
            )


def _page(row: tuple[Any, ...]) -> NotebookPage:
    return NotebookPage(
        organization_id=row[0],
        node_id=row[1],
        kind=row[2],
        path=row[3],
        summary=row[4] or "",
        summary_version=row[5],
        based_on=row[6] or {},
        needs_revalidation=row[7],
        updated_at=row[8],
    )
