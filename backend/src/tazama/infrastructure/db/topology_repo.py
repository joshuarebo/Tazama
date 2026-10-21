"""SQLAlchemy adapters for the site-identification ports.

Two things here deserve attention:

  * **Name search takes untrusted text.** The tokens come from a customer's complaint, so
    they are bound as parameters and their LIKE metacharacters are escaped. No pattern is
    ever built by concatenating input into SQL.
  * **The upstream walk is a recursive CTE** over `topology_edge`, which both SQLite and
    Oracle support. It is depth-bounded, which also makes a cyclic edge table harmless
    instead of fatal.

Both adapters take an optional `Session` so a read can join the caller's transaction and
see its uncommitted writes; without one they open their own. Every query names its columns
(no `SELECT *`) and carries a row limit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from sqlalchemy import Integer, String, func, literal, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tazama.core.logging import get_logger
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import Cell, Site, TopologyEdge, TopologyNode
from tazama.modules.site_identification.models import (
    CellInfo,
    NameMatch,
    SiteInfo,
    TopologyHop,
)

_log = get_logger(__name__)

T = TypeVar("T")

#: Hard ceiling on any single read, whatever the caller asks for. An unbounded fan-out
#: driven by complaint text would be a denial of service against ourselves.
MAX_ROWS = 500

#: Tokens shorter than this are not searched: a two-letter LIKE matches most of the estate.
MIN_TOKEN_LEN = 3

#: Most tokens we will put in one OR-chain. Beyond this the query stops being selective.
MAX_SEARCH_TOKENS = 8

#: Default depth of the upstream walk: cell -> site -> transmission -> aggregation -> core
#: is four hops, so six leaves headroom without letting a bad edge table run away.
DEFAULT_UPSTREAM_DEPTH = 6


class _SessionBound:
    """Run on the caller's session, or open one per call."""

    __slots__ = ("_engine", "_session")

    def __init__(self, session: Session | None = None, *, engine: Engine | None = None) -> None:
        self._session = session
        self._engine = engine

    def _read(self, work: Callable[[Session], T]) -> T:
        if self._session is not None:
            return work(self._session)
        with session_factory(self._engine or get_engine())() as session:
            return work(session)


def _escape_like(token: str) -> str:
    """Neutralise LIKE metacharacters in untrusted text.

    Without this, a complaint containing `%` would match every site in the country — a
    cheap table scan triggered by a customer typing a punctuation mark.
    """
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _searchable(tokens: Sequence[str]) -> list[str]:
    """The tokens worth searching: long enough, deduplicated, bounded in number."""
    seen: dict[str, None] = {}
    for token in tokens:
        cleaned = token.strip().casefold()
        if len(cleaned) >= MIN_TOKEN_LEN:
            seen.setdefault(cleaned, None)
    return list(seen)[:MAX_SEARCH_TOKENS]


def _bounded(limit: int) -> int:
    return max(1, min(limit, MAX_ROWS))


class SqlSiteDirectory(_SessionBound):
    """Names and geography of sites and cells."""

    def cells_for_region(self, region: str, *, limit: int = 200) -> tuple[CellInfo, ...]:
        def work(session: Session) -> tuple[CellInfo, ...]:
            stmt = (
                select(
                    Cell.id,
                    Cell.site_id,
                    Cell.name,
                    Cell.region,
                    Cell.lat,
                    Cell.lon,
                    Cell.tech,
                    Cell.status,
                )
                .where(Cell.region == region)
                # A decommissioned cell cannot be the answer to "where is this customer".
                .where(Cell.status == "active")
                .order_by(Cell.id)
                .limit(_bounded(limit))
            )
            return tuple(_cell(row) for row in session.execute(stmt))

        return self._read(work)

    def search_by_name(
        self,
        tokens: Sequence[str],
        *,
        limit: int = 50,
    ) -> tuple[NameMatch, ...]:
        """Sites, cells and regions whose names contain any of these tokens.

        Deliberately generous: the service ranks the results by token overlap, so a wide
        net here costs one bounded query and buys a better answer.
        """
        terms = _searchable(tokens)
        if not terms:
            return ()
        row_cap = _bounded(limit)

        def work(session: Session) -> tuple[NameMatch, ...]:
            matches: list[NameMatch] = []

            site_stmt = (
                select(Site.id, Site.name, Site.region)
                .where(_name_filter(Site.name, terms))
                .order_by(Site.id)
                .limit(row_cap)
            )
            matches += [
                NameMatch(kind="site", id=row.id, name=row.name, region=row.region)
                for row in session.execute(site_stmt)
            ]

            cell_stmt = (
                select(Cell.id, Cell.name, Cell.region, Cell.site_id)
                .where(_name_filter(Cell.name, terms))
                .where(Cell.status == "active")
                .order_by(Cell.id)
                .limit(row_cap)
            )
            matches += [
                NameMatch(
                    kind="cell",
                    id=row.id,
                    name=row.name,
                    region=row.region,
                    site_id=row.site_id,
                )
                for row in session.execute(cell_stmt)
            ]

            # Regions are not a table, so they are read as the distinct values actually
            # present — that way a renamed region needs no code change.
            region_stmt = (
                select(Site.region)
                .where(_name_filter(Site.region, terms))
                .group_by(Site.region)
                .order_by(Site.region)
                .limit(row_cap)
            )
            matches += [
                NameMatch(kind="region", id=row.region, name=row.region, region=row.region)
                for row in session.execute(region_stmt)
            ]
            return tuple(matches[:row_cap])

        return self._read(work)

    def get_cell(self, cell_id: str) -> CellInfo | None:
        def work(session: Session) -> CellInfo | None:
            stmt = (
                select(
                    Cell.id,
                    Cell.site_id,
                    Cell.name,
                    Cell.region,
                    Cell.lat,
                    Cell.lon,
                    Cell.tech,
                    Cell.status,
                )
                .where(Cell.id == cell_id)
                .limit(1)
            )
            row = session.execute(stmt).first()
            return None if row is None else _cell(row)

        return self._read(work)

    def get_site(self, site_id: str) -> SiteInfo | None:
        def work(session: Session) -> SiteInfo | None:
            stmt = (
                select(
                    Site.id,
                    Site.name,
                    Site.region,
                    Site.lat,
                    Site.lon,
                    Site.parent_node_id,
                )
                .where(Site.id == site_id)
                .limit(1)
            )
            row = session.execute(stmt).first()
            if row is None:
                return None
            return SiteInfo(
                id=row.id,
                name=row.name,
                region=row.region,
                lat=row.lat,
                lon=row.lon,
                parent_node_id=row.parent_node_id,
            )

        return self._read(work)


class SqlTopologyReader(_SessionBound):
    """Reads the network graph: siblings, sectors, and what sits upstream."""

    def siblings_of_cell(self, cell_id: str, *, limit: int = 50) -> tuple[CellInfo, ...]:
        """Other cells on the same site. One correlated subquery, not two round trips."""

        def work(session: Session) -> tuple[CellInfo, ...]:
            site_of = select(Cell.site_id).where(Cell.id == cell_id).scalar_subquery()
            stmt = (
                select(
                    Cell.id,
                    Cell.site_id,
                    Cell.name,
                    Cell.region,
                    Cell.lat,
                    Cell.lon,
                    Cell.tech,
                    Cell.status,
                )
                .where(Cell.site_id == site_of)
                .where(Cell.id != cell_id)
                .order_by(Cell.id)
                .limit(_bounded(limit))
            )
            return tuple(_cell(row) for row in session.execute(stmt))

        return self._read(work)

    def cells_under_site(self, site_id: str, *, limit: int = 50) -> tuple[CellInfo, ...]:
        def work(session: Session) -> tuple[CellInfo, ...]:
            stmt = (
                select(
                    Cell.id,
                    Cell.site_id,
                    Cell.name,
                    Cell.region,
                    Cell.lat,
                    Cell.lon,
                    Cell.tech,
                    Cell.status,
                )
                .where(Cell.site_id == site_id)
                .order_by(Cell.id)
                .limit(_bounded(limit))
            )
            return tuple(_cell(row) for row in session.execute(stmt))

        return self._read(work)

    def upstream_of(
        self,
        node_id: str,
        *,
        max_depth: int = DEFAULT_UPSTREAM_DEPTH,
    ) -> tuple[TopologyHop, ...]:
        """Walk `topology_edge` towards the core, nearest hop first.

        A recursive CTE rather than a loop of queries: one round trip, and the database
        does the walking. `topology_node` supplies the kind and name, outer-joined because
        a site or cell may appear as an edge endpoint without its own node row.
        """
        depth = max(1, min(max_depth, 12))

        def work(session: Session) -> tuple[TopologyHop, ...]:
            seed = (
                select(
                    TopologyEdge.parent_id.label("node_id"),
                    literal(1, Integer).label("depth"),
                )
                .where(TopologyEdge.child_id == node_id)
                .cte("upstream", recursive=True)
            )
            step = (
                select(
                    TopologyEdge.parent_id.label("node_id"),
                    (seed.c.depth + literal(1, Integer)).label("depth"),
                )
                .select_from(TopologyEdge)
                .join(seed, TopologyEdge.child_id == seed.c.node_id)
                .where(seed.c.depth < depth)
            )
            walk = seed.union_all(step)
            stmt = (
                select(
                    walk.c.node_id,
                    func.min(walk.c.depth).label("depth"),
                    func.min(TopologyNode.kind).label("kind"),
                    func.min(TopologyNode.name).label("name"),
                )
                .select_from(walk)
                .outerjoin(TopologyNode, TopologyNode.id == walk.c.node_id)
                # A diamond in the graph can reach the same node twice; grouping keeps the
                # nearest depth and one row per node.
                .group_by(walk.c.node_id)
                .order_by(func.min(walk.c.depth), walk.c.node_id)
                .limit(MAX_ROWS)
            )
            return tuple(
                TopologyHop(
                    node_id=row.node_id,
                    kind=row.kind or "",
                    name=row.name or "",
                    depth=int(row.depth),
                )
                for row in session.execute(stmt)
            )

        return self._read(work)


# ------------------------------------------------------------------------ helpers


def _name_filter(column: Any, terms: Sequence[str]) -> Any:
    """`lower(name) LIKE '%token%'` for any token, with every value bound.

    `func.lower` rather than `ilike`: Oracle has no `ILIKE`, and lowering both sides is the
    one form that behaves the same on both backends.
    """
    lowered = func.lower(column)
    return or_(
        *[lowered.like(literal(f"%{_escape_like(term)}%", String), escape="\\") for term in terms],
    )


def _cell(row: Any) -> CellInfo:
    return CellInfo(
        id=row.id,
        site_id=row.site_id,
        name=row.name,
        region=row.region,
        lat=row.lat,
        lon=row.lon,
        tech=row.tech,
        status=row.status or "active",
    )
