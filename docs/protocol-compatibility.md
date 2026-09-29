# Protocol compatibility

Agent WebSockets have an explicit protocol handshake before they become active Gateway connections.

## Version range

The current protocol range is v1 only. New Agents send `protocol_min` and `protocol_max` in `AgentHello`; the Gateway selects the highest common version. Legacy Agents that omit both fields are interpreted as v1 for rolling-upgrade compatibility.

A reversed range is invalid. A range with no overlap is rejected with WebSocket close code `4406`.

## Promotion boundary

Authentication alone does not make a socket an active Agent. The first application message must be `hello`, and the Gateway validates:

- path Agent ID matches `hello.agent_id`
- hello payload is structurally valid
- protocol ranges overlap
- capability names satisfy the existing bounded schema

Only after those checks does the Gateway replace any existing connection for the same Agent ID. Therefore a malformed or incompatible reconnect cannot evict a healthy Agent.

After promotion, duplicate hello messages are rejected. Heartbeats are parsed and must carry the same Agent ID.

`list_devices` and `device_info` expose the negotiated `protocol_version` together with package version and capabilities.
