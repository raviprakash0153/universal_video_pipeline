# Real-world evaluation bundle (Phase 5B.7 foundation)

This format is a local, versioned container for a future manually reviewed
evaluation. The implementation supports schema version `1.0.0` in
`evaluation_bundle.py`; validation checks structure, types, supported states,
URL syntax, timestamps, and safe evidence-reference syntax only. It does not
fetch data, open evidence files, assign labels, or measure quality. Empty
bundles are valid and are the appropriate starting point. No real-world quality
baseline is available from this foundation.

Capture and retrieval timestamps must be timezone-aware ISO 8601 strings. Web
source URLs must parse as HTTP or HTTPS URLs with a host. Publisher identity
must be a domain-like host or the explicit state `unknown` or
`not_assessable`; a missing publisher record is represented as `null`. Unknown
identity does not mean independent sourcing. URL syntax checks do not establish
source credibility or independence. A Google News RSS URL cannot serve as
publisher evidence; keep feed information in `discovery_source` and record the
resolved publisher separately when known.

Evidence references use a non-empty relative POSIX path such as
`evidence/notes/item-1.txt`, with no absolute paths, `.` or `..` components,
empty path components, drive prefixes, URL forms, or backslashes. Validation
does not resolve, open, or read those paths. `write_bundle` creates new files
exclusively by default and raises `BundleExistsError` if the destination exists;
pass `overwrite=True` to explicitly replace it.

## Bundle layout

Bundles are UTF-8 JSON. `schema_version` versions this contract. `capture`
records an ISO 8601 timestamp and its timezone; `project.code_version` records
the code identifier. `scope` contains selected research categories and one of
`india`, `global`, or `india_and_global`. `discovery` contains query records and
provider/query metadata. `candidates` may be empty. Each candidate keeps
`discovery_provenance` distinct from `annotations.geographic_relevance`, and
keeps `pipeline_predictions` separate from human `annotations`. Structural
separation is enforced: annotation fields have a defined schema, and
unexpected fields are rejected. The validator cannot establish whether a human
reviewer was genuinely independent. Reviewer independence is procedural:
reviewers should label records without seeing pipeline predictions. Retain
each review and any adjudication or reviewer disagreement transparently; do not
infer or copy annotations from predictions. Source records retain discovery
source, source/resolved URLs,
publisher metadata (including original publisher when known), retrieval time,
HTTP and extraction outcomes, and an optional local evidence reference.
Unavailable source values are explicit `null`; unavailable evidence is
`evidence_reference: null`, with the extraction outcome explaining why where
known. Do not store complete publisher articles; use permitted local notes,
short lawful excerpts where applicable, or metadata references.

Google News RSS is recorded only as `discovery_source` provenance. It is not
publisher evidence and must not be treated as the publisher. Record the resolved
publisher URL and identity when available. Respect CAPTCHA, 403, 429, robots,
terms, and other access restrictions; record the outcome and stop. Never bypass
access controls.

`evaluation.provenance` records who/what produced the evaluation and
`evaluation.limitations` lists known gaps. Keep original records and reviewer
annotations attributable. Multiple reviewers should have separate records or
bundles; retain disagreements and adjudication transparently.

## Human annotation rubric

Annotators work from permitted source evidence and metadata, without viewing or
using `pipeline_predictions` to decide labels. Record the reviewer identity and
use the explicit states below; absence of a label is not a negative label.

1. **Story identity and duplicates:** assign the same `story_group_id` only
   when records describe the same underlying event/update. Similar topic alone
   is insufficient. Keep distinct updates/events separate and note uncertainty.
2. **Relevant categories:** independently assess each in-scope category as
   `relevant`, `not_relevant`, `unknown`, or `not_assessable`. Unknown means
   evidence is insufficient to decide; not-assessable means the item cannot be
   judged under this protocol. Neither means negative.
3. **India/global relevance:** choose `india`, `global`, `both`, `neither`,
   `unknown`, or `not_assessable` based on the story's substantive relevance,
   not its discovery query, feed, publisher location, or pipeline prediction.
4. **Primary-source status:** `yes` only for a source with direct first-hand
   evidence/authority for the reported claim; otherwise `no`, `unknown`, or
   `not_assessable`. A discovery feed is never the primary source.
5. **Evidence sufficiency:** `sufficient` when permitted evidence supports the
   assessed claim and identity; `insufficient` when available evidence does
   not; use `unknown` or `not_assessable` where appropriate.
6. **Independent corroboration:** count only distinct reporting/evidence
   origins. Reprints, shared wire copy, and syndicated articles are not
   independent corroboration. Use `independently_corroborated`,
   `not_corroborated`, `unknown`, or `not_assessable`.
7. **Source identity and syndication:** mark identity `known`, `unknown`, or
   `not_assessable`; mark syndication `original`, `syndicated`,
   `not_syndicated`, `unknown`, or `not_assessable`. Preserve outlet and known
   original publisher separately in source metadata.
8. **Assessability and uncertainty:** select `assessable`,
   `partially_assessable`, `not_assessable`, or `uncertain`; list concrete
   uncertainties. Do not convert inaccessible/missing evidence into a negative.

## Future evaluation procedure and limits

For a future study, freeze the sampling window, categories, coverage mode,
queries/providers and code version; capture candidates and discovery metadata
locally; retain source outcomes without circumventing restrictions; then have
reviewers independently annotate candidates without seeing pipeline labels.
Resolve disagreements with a documented adjudication step while retaining
original annotations. Only then compare predictions to assessable expected
labels, report unknown/not-assessable rates, sampling and missing-evidence
limits, and stratify results by category and geography. Deduplicate stories and
identify syndicated origins before counting independent corroboration.

Structural validation is distinct from the reviewer protocol and from real-world
quality evidence. The reviewer protocol requires blind labeling and transparent
retention of disagreements; the schema cannot verify that process. No
real-world quality evidence or baseline exists yet.

This foundation does not specify a sampling design, adjudication policy,
inter-rater statistic, or metric threshold. Those choices must be preregistered
for the intended use before any real-world baseline is claimed. Structural
tests in `tests/test_evaluation_bundle.py` use synthetic objects and establish
serialization/validation behavior only; they are not real-world quality
results. The existing research pipeline is not changed or invoked by this
format.
