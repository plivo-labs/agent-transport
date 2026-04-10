# Local Development

## Prerequisites

- **Rust** (stable toolchain)
- **CMake** (for audiopus_sys / aws-lc-sys)
- **Python 3.10+** (for the Python SDK)
- **Node 20+** (for the Node SDK)

On CMake 4.x, set `CMAKE_POLICY_VERSION_MINIMUM=3.5` in your environment.

## Building

### Rust core

```bash
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
```

### Python SDK

```bash
cd crates/agent-transport-python
pip install -e ".[all]"
```

This builds the Rust binding via maturin and installs the Python adapters in editable mode. Changes to `.py` files in `adapters/agent_transport/` are reflected immediately.

### Node SDK

```bash
cd crates/agent-transport-node
npm install
npm run build:all    # builds native binary + TypeScript adapters
```

To rebuild only the TypeScript adapters after editing:

```bash
npm run build:livekit
```

## Running Examples

### Python

```bash
pip install -e "crates/agent-transport-python[all]"
SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/livekit/sip_agent.py
```

### Node

```bash
cd crates/agent-transport-node && npm install && npm run build:all && cd ../..
SIP_USERNAME=xxx SIP_PASSWORD=yyy npx tsx examples/livekit/sip_agent.ts
```

## Project Layout

Adapter source lives inside each crate directory:

- **Python**: `crates/agent-transport-python/adapters/agent_transport/`
- **Node**: `crates/agent-transport-node/adapters/livekit/` (TS source) -> `livekit/` (compiled JS, gitignored)

No copy steps are needed. CI builds directly from these locations.
