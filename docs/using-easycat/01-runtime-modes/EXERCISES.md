# Chapter 1 Exercises

## Compare ownership, not just syntax

1. Run `local`, stop it, then run `browser` and have one short conversation in
   each mode.
2. Without changing `build_app()`, run `websocket`. Note what is missing for a
   human user and identify which side now owns the client UI and wire protocol.
3. Read the Twilio prerequisites without starting the server. Write down why a
   local microphone needs no public URL while an inbound phone call does.

Self-check:

- The agent behavior is identical in local and browser mode because the same
  specification is adapted for both.
- WebSocket mode deliberately supplies no human UI; the connecting application
  owns that client.
- A Twilio media stream originates outside your network, so its `wss://` URL
  must be publicly reachable and its webhook must be authenticated.
- `browser`, `websocket`, and `twilio` create a fresh session per connection;
  they do not share one live agent bridge or provider stream.

For a security stretch, try constructing
`VoiceApp(agent="openai", host="0.0.0.0", serve_token=...)` with a fresh
`config_factory`, then run browser mode without a `serve_token`. Read the
non-loopback bind error, then stop—do not use the unsafe override merely to
silence the guard. In a real deployment, load the token from a secret store.
