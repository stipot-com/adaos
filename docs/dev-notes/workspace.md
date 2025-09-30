# Workspace monorepo lifecycle

`GitSkillRepository` and `GitScenarioRepository` share the same sparse-checkout workspace.
The lifecycle is intentionally symmetrical:

1. Ensure the repository exists (clone on demand) and initialise sparse-checkout in *no-cone* mode.
2. Update the sparse pattern file with the requested `skills/<name>` or `scenarios/<name>` entry.
3. Pull the repository and wait for the directory plus manifest to materialise before returning metadata.
4. During uninstall remove the directory, drop the path from the sparse patterns, run `git rm --cached` for
   the sub-tree and reapply sparse checkout. The command is idempotent — calling uninstall twice is safe.
5. Whenever the sparse pattern list becomes empty we keep the workspace clean by reapplying sparse-checkout
   with an empty pattern list.

This flow guarantees that a follow-up `install → uninstall → install` round-trip does not leave untracked
files or stale sparse patterns, which was the root cause of `FileNotFoundError: ... not present after sync`.
