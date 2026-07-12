# Publishing sonilo-mcp to the MCP Registry + directories

Goal: make `sonilo-mcp` discoverable everywhere agent developers look for MCP servers. The official MCP Registry is the master copy — the big aggregators (Glama, PulseMCP, and MCP clients) sync from it.

## One-time sequence (order matters)

### 1. Release 0.2.1 to PyPI first

The registry verifies PyPI ownership by finding the `mcp-name` comment in the package description **on PyPI**. This repo's README now carries `<!-- mcp-name: io.github.sonilo-ai/sonilo-mcp -->`, but the check runs against the published package — so cut a release that includes it:

- bump `pyproject.toml` version to `0.2.1`
- release to PyPI as usual (the existing release workflow)

### 2. Publish to the official MCP Registry

Requires a GitHub account that is a member of the `sonilo-ai` org (the `io.github.sonilo-ai/*` namespace is verified through GitHub auth).

```bash
# install the publisher CLI
brew install mcp-publisher
# or: curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/

# from the repo root (server.json is already here)
mcp-publisher login github
mcp-publisher publish
```

Verify: https://registry.modelcontextprotocol.io/v0/servers?search=sonilo

If the schema has moved since this file was written, `mcp-publisher publish` will say so — regenerate with `mcp-publisher init` and re-apply the values from the committed `server.json`.

### 3. Directories that need a one-off manual touch

| Directory | Action | Time |
|---|---|---|
| [Glama](https://glama.ai/mcp/servers) | Syncs from the registry + crawls GitHub; after step 2, claim the server via "Claim" (GitHub sign-in) to control the listing | 2 min |
| [Smithery](https://smithery.ai) | `smithery.yaml` is committed in this repo; sign in with GitHub on smithery.ai and add the repo | 3 min |
| [PulseMCP](https://www.pulsemcp.com) | Hand-reviewed directory; submit via their "Submit a server" form | 2 min |
| [mcp.so](https://mcp.so) | Submit via the site's Submit form (GitHub sign-in) | 2 min |

### 4. When new versions ship

`mcp-publisher publish` again with the bumped version in `server.json` (keep `packages[0].version` in sync with `pyproject.toml`). Consider wiring it into the release workflow — the registry repo documents a GitHub Actions setup (`docs/modelcontextprotocol-io/github-actions.mdx`).
