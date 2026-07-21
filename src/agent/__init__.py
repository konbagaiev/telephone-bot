"""The conversational agent: how the Realtime model is configured and how its
tool calls are turned into facts.

The model owns speech; this package owns facts (ADR-002). `session` holds the
Realtime session configuration and the tool definitions the model may call;
`tools` turns a tool call into a write against the operational data. Neither
opens a socket — the transport is the bridge's job, so both stay testable without
the network.
"""
