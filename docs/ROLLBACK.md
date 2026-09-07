# Rollback

NAR is additive by design. It never modifies: the ZCode desktop install,
`~/.zcode/*` configs (read-only access only), `~/.codex/*`, `~/.claude/*`,
`~/.config/opencode/*`, or any project outside the workspaces you point at.

## Uninstall

```bash
pip uninstall native-agent-router        # removes nar / nar-mcp
```

## Remove all state (tasks, logs, stats, locks)

```bash
# default home:
rm -rf ~/.native-agent-router            # PowerShell: Remove-Item -Recurse $env:USERPROFILE\.native-agent-router
# or wherever NAR_HOME points
```

If you used `zcode_home:"isolated"`, deleting the home also removes the
on-disk key copy under `<home>/zcode-home/`.

## Config

Your agents.json lives in the home dir above; deleting it (or the home) reverts
to auto-discovery defaults. Nothing was written into Codex/opencode/ZCode
configs — if you added an MCP entry yourself (per README), remove that block.

## In-flight agents on rollback

Before uninstalling, drain: `nar list` → for anything `running|blocked`:
`nar cancel <id>` and check `confirmed`, else `nar kill <id> --yes`.
Workspace locks are files under `<home>/locks/`; removing the home clears them.

## This repository

Development is a git repo with tagged rollback points
(`git log --oneline`). Reverting to any commit restores code state; the runtime
home is separate and untouched by git operations.

## Upstream reference projects (if you tried them)

NAR does not install or depend on openclaw/acpx, coder-mcp-bridge,
zcode-acp or zcode-open-bridge. If you installed any of those separately,
remove them with their own installers' rollback docs (e.g. the codex-zcode-bridge
project keeps its own backups/ and ROLLBACK.md).
