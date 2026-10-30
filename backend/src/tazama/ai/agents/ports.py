"""What the agents need from a model, declared where they need it.

The agents used to import `LlmGateway` directly, which the import-linter caught: it made
every agent a transitive importer of `httpx`, and the contract "only the gateway talks to a
model endpoint" existed precisely to stop that.

The contract was right and the fix is the ports-and-adapters one — the consumer declares
the interface, the composition root supplies the implementation. It is not a formality:

* An agent can now be tested with a three-line fake instead of a mock transport.
* "Can an agent reach the network?" is answerable by reading this file. Nothing here
  exposes a URL, a client, or a retry.
* A second model path (a local classifier, a different vendor) becomes an adapter rather
  than an edit to three agents.

`LlmGateway` satisfies `LlmPort` structurally, so nothing had to change at the call sites.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tazama.ai.llm.schemas import StrictModel


@runtime_checkable
class LlmResponse(Protocol):
    """The parts of a model result an agent is allowed to care about.

    Notably absent: the raw text. An agent reads `parsed`, which is schema-validated, or it
    treats the call as degraded. Letting an agent fall back to raw text would route around
    the validation that makes the output safe to act on.
    """

    @property
    def parsed(self) -> StrictModel | None: ...

    @property
    def degraded(self) -> bool: ...

    @property
    def note(self) -> str: ...


@runtime_checkable
class LlmPort(Protocol):
    """The one call an agent makes.

    `configured` exists so an agent can choose its deterministic path *before* paying for a
    round trip, which is what makes "the product works with the model down" cheap rather
    than merely possible.
    """

    @property
    def configured(self) -> bool: ...

    def complete(
        self,
        *,
        task: str,
        system: str,
        user: str,
        untrusted: str = "",
        prompt_version: str = "v1",
        fast: bool = False,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 800,
    ) -> Any: ...
