# Module boundaries

## The rule
A module in `backend/src/tazama/modules/<name>/` may import:

* `tazama.core.*` (settings, logging, errors, security, tracing, ids, clock)
* `tazama.domain.*` (entities, value objects, events, state machine)
* its own package

It may **not** import another module, `tazama.infrastructure`, `tazama.api` or
`tazama.worker`. Two mechanisms replace those imports:

1. **Ports.** The module declares the interface it needs in its own `ports.py` as a
   `Protocol`. An adapter in `infrastructure/` satisfies it structurally — the adapter does
   not import the port, and the module does not import the adapter. `api/deps.py` (the
   composition root) wires them.
2. **Domain events.** Cross-module reactions happen by publishing an event, never by
   calling another module's service.

## Why
This is the only thing that keeps a modular monolith from becoming a ball of mud. It is
also what makes the pipeline testable: every module can be exercised with in-memory fakes
and a `FixedClock`, with no database and no network.

## Enforcement
`[tool.importlinter]` in `backend/pyproject.toml` declares five contracts, checked by
`make lint` / `make boundaries` and in CI:

1. `domain` is pure — no infrastructure, no framework, no SQLAlchemy, no HTTP.
2. Modules are mutually independent.
3. Modules do not reach into infrastructure or the API layer.
4. Only the LLM gateway talks to a model endpoint (`httpx` is forbidden elsewhere).
5. The layering above is respected.

## Worked example
Correlation needs recent KPIs and the topology, and must publish `IncidentDetected`:

```python
# modules/correlation/ports.py
class KpiReader(Protocol):
    def recent(self, cell_id: str, since: datetime) -> Sequence[KpiSample]: ...

class TopologyReader(Protocol):
    def upstream_of(self, cell_id: str) -> Sequence[str]: ...

class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> None: ...

# modules/correlation/service.py
class CorrelationService:
    def __init__(self, kpis: KpiReader, topology: TopologyReader, events: EventPublisher,
                 clock: Clock, policy: CorrelationPolicy) -> None: ...
```

The Oracle and SQLite repositories implement `recent()` and `upstream_of()`; the outbox
implements `publish()`. Correlation knows none of them, so a test can hand it three
fakes and assert the decision rather than the plumbing.

## When the boundary feels wrong
If a module seems to need another module's logic, one of these is true: the logic belongs
in `domain` (pure, shared), it belongs behind a port, or the two modules are really one.
Do not add a cross-module import and a tech-debt note — fix the shape.
