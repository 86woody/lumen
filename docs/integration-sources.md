# Integration evidence

Consulted 2026-09-11:

- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): newline-delimited UTF-8 JSON-RPC; stdout contains protocol messages only.
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools): four named tools advertised with input schemas; tool failures return isError; management operations excluded.
- [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle): initialize negotiation and initialized notification precede normal tool calls.
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli): installation documentation consulted; no installed Codex CLI support claimed yet.

Local `claude --version`: 2.1.268. Supported `claude auth status --json`:
loggedIn=true, authMethod=claude.ai, apiProvider=firstParty,
subscriptionType=max. Account-identifying fields were not retained. Overage
configuration remains unverified. No inference has been executed.

The Firecrawl skill was inspected but its credit-based workflow is not used:
the user forbids paid credits and separately billed interfaces. Public official
documentation was read with the provided browsing tool instead.
