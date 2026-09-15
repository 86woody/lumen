# Shared-checkout admission

Status: implemented component; Release 1 authority acceptance pending.

File format, recorded 2026-09-12: a shared event file is the line `# Lumen event`,
a blank line, and the event's canonical JSON inside a fenced `json` block, nothing
else. The reader rejects any other bytes, a noncanonical body, or a filename that
is not the event id. The plan's example showed YAML front matter; the runtime has
no YAML parser and accepts no dependency for one, so the JSON fence is the contract
and the plan was amended to match.

Before recall and owner reindex, inspect only granted folders in the configured
workspace's `.lumen/shared/`. Reuse the canonical file reader, closure validation,
deletion journal and source-policy-bound local destination receipts. Agent requests
cannot create receipts. A missing receipt returns `index_pending` before retrieval.

Check every recall, including the first after restart, so timestamp caching cannot
hide a changed file or revoked receipt. Already admitted events do not create
new events or rebuild the index. New bundles commit through the existing reviewed
import transaction. No model, network call or publication participates.

Human review retains the 100-event/256-KiB bound. Checkout admission accepts up to
10,000 events/64 MiB across independently reviewed batches. Source proofs now bind
each event ID, scope and digest, allowing receipt validation without loading an
ungranted shared folder or copying event payloads into new control records. Lookup
is limited to 1,000 receipt candidates and 16 MiB of accepted proof data. Existing
proofs lacking membership support can establish only exact-bundle admission.

A 500-ms post-scan check, also checked between review candidates,
refuses late results; this does not interrupt filesystem I/O or prove an end-to-end
latency bound. CODEOWNERS/PR provenance and measured deadlines
remain mandatory work. Local receipt validation does not establish those gates.
