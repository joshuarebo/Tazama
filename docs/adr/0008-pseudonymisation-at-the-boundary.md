# ADR 0008 — Pseudonymise at the ingestion boundary, not at the edge of each feature

Status: accepted · Date: 2026-10-21 · Phase: 0

## Context
Kenya's Data Protection Act 2019 and the PRD both require that raw subscriber identifiers
do not reach analytics, AI prompts, logs, exports or Teams cards. Masking at each point of
use means every new feature is a new chance to leak.

## Decision
MSISDNs are normalised and immediately converted to an HMAC-SHA256 pseudonym
(`PSEUDONYM_SALT`) at ingestion. Downstream, the pseudonym **is** the identifier: the
canonical schema stores `subscriber_token`, and nothing below ingestion accepts a raw
MSISDN. Care-agent lookup takes a pseudonym (or normalises and hashes the input in the
request handler, never storing the raw value).

Defence in depth: the log formatter and the LLM redaction layer also mask MSISDN-shaped
strings, so an accidental raw value is scrubbed rather than persisted.

## Consequences
Re-identification requires the salt plus a candidate MSISDN (the design intent). Rotating
the salt invalidates historical grouping, so rotation is a documented, deliberate
operation. HMAC is not reversible, so "show me the real number" is a CRM question, not a
TAZAMA one — which is the privacy outcome we want.
