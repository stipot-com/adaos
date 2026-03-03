# Join-codes (phase 1) and mTLS notes

## Join-code semantics (phase 1)

A join-code is a short secret (typically `XXXX-YYYY`) used **once** to bootstrap a member node.

Properties:

- **One-time:** the hub invalidates the code on first successful use.
- **TTL:** default 15 minutes (`--ttl-min`), max 60 minutes.
- **Not a long-lived token:** the join-code is only used to exchange for the actual subnet connection parameters.

Storage:

- Hub keeps join-code state under `$ADAOS_BASE_DIR/join_codes.json`.
- Member never stores the join-code after a successful join.

Threat model notes:

- Treat join-codes like passwords during their TTL.
- Don’t send them to shared logs or chats unless necessary.

## What is delivered to the member

In phase 1, the join exchange returns:

- `subnet_id`
- subnet API token (stored in `node.yaml` as `token`)
- hub URL (stored in `node.yaml` as `hub_url`)

This avoids passing the subnet token in the command line while still enabling existing WS/HTTP authentication.

## mTLS (current state)

Phase 1 join-code flow focuses on safe token *delivery* without exposing tokens in CLI arguments.

mTLS provisioning/rotation is expected to be layered on top in a later phase (e.g., issuing per-node client certificates and using them for member↔hub or node↔root authentication).

