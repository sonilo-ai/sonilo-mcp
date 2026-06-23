# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `sonilo-mcp`, please report it
privately so we can address it before public disclosure.

**Email:** [info@sonilo.com](mailto:info@sonilo.com)

Please include:

- A description of the issue and its potential impact
- Steps to reproduce (a minimal proof of concept is ideal)
- The affected version (`sonilo-mcp --version` or the PyPI version you installed)
- Any suggested remediation, if you have one

Please **do not** open a public GitHub issue for security reports.

## What to Expect

- We aim to acknowledge your report within a few business days.
- We will keep you informed as we investigate and work on a fix.
- Once a fix is released, we are happy to credit you for the discovery
  (unless you prefer to remain anonymous).

## Scope

This policy covers the `sonilo-mcp` MCP server in this repository. Issues in
the hosted Sonilo API or platform should also be reported to
[info@sonilo.com](mailto:info@sonilo.com).

## Handling of Credentials

`sonilo-mcp` reads your `SONILO_API_KEY` from the environment / MCP client
configuration. This key is sent only to the configured `SONILO_API_URL` as a
Bearer token and is never logged or written to disk. Treat your MCP client
configuration file as sensitive, since it stores this key in plaintext.
