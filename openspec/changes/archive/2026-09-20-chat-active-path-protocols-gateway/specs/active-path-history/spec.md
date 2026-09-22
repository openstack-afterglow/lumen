## ADDED Requirements

### Requirement: Indexed active-path projection
Lumen MUST preserve the immutable message parent graph while storing one ordered root-to-leaf active-path projection and monotonic revision per conversation. Normal resume, context, and message-page reads MUST use the projection instead of recursively reconstructing every branch. Every append, completion, retry, regeneration, fork, and descend mutation MUST update graph rows and projection in one locked transaction.

#### Scenario: New message advances the active projection
- **WHEN** an accepted conversation mutation appends a message to the active leaf
- **THEN** the message graph, ordered projection, active leaf, and revision commit atomically

#### Scenario: Projection invariant cannot be established
- **WHEN** backfill or mutation finds a cycle, missing parent, cross-conversation edge, or disconnected selected leaf
- **THEN** Lumen fails closed without publishing a partial projection

### Requirement: Revision-fenced opaque history pages
Lumen MUST return bounded active-path message pages with opaque signed cursors that bind the conversation, direction, position, limit, and active-path revision. A cursor from an older revision MUST return conflict rather than silently paging a different branch. The response MUST include only server-projected sibling metadata needed to select another branch.

#### Scenario: Cursor becomes stale after branch change
- **WHEN** a caller reuses a page cursor after the active path revision changes
- **THEN** Lumen returns a conflict that instructs the client to reload the latest page

#### Scenario: Caller descends to a sibling branch
- **WHEN** a valid sibling message is selected with descend enabled
- **THEN** Lumen validates the sibling relationship, updates the active projection transactionally, increments the revision, and returns the new active path
