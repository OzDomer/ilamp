# 0001: One server process is the only thing that talks to the lamp

Status: accepted, 2026-10-01

## Context

The lamp accepts a single BLE controller at a time and stops advertising while connected. Several inputs are planned: a web UI (phone and PC), a clap detector, voice commands, and later a projector-wall HUD. Each of them could open its own `Lamp`, but only one of them would ever get the connection, and the others would see "lamp not found".

The lamp also only reports state *changes*. Whoever holds the connection is the only one who knows the current state.

## Decision

A single long-running hub process owns the one `Lamp` and the BLE connection. Everything else is a client that talks to the hub over a WebSocket with JSON messages. The hub validates every request before it reaches the lamp, handles reconnects, and pushes the lamp's reported state to all clients whenever it changes.

Inputs (UI, claps, voice) never import `ilamp` directly.

## Alternatives considered

- **Each input opens its own `Lamp`.** Impossible with a one-controller lamp; they would fight over the connection.
- **A shared `Lamp` object inside one process with all inputs as threads/tasks.** Works until an input needs to live elsewhere (a phone, another machine, an ESP32). A socket boundary costs little and keeps the door open.
- **HTTP polling instead of a WebSocket.** Simpler, but clients would not learn about changes made by other inputs until they polled; a WebSocket lets the hub push state the moment the lamp reports it.

## Consequences

- The hub is a single point of failure, and that is fine: so is the lamp.
- State is owned by the lamp and relayed by the hub; clients render what they are told and never assume a command worked.
- The `ilamp` library stays a library: no server code inside it.
- Adding an input means writing a WebSocket client, nothing more.
