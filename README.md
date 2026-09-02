# atv-homekit-remote

Pure-Python HomeKit **Target Controller** + **HomeKit Data Stream (HDS)** library for Apple TV remote buttons and Siri voice input.

This ports the core behavior of [`marcusadolfsson/appletv-siri-voice`](https://github.com/marcusadolfsson/appletv-siri-voice) from Node/HAP-NodeJS to Python. It uses HAP-python for HomeKit pairing/encrypted HAP transport and implements the missing Target Control and HDS protocol layers in this package.

## What it supports

- HomeKit Target Controller accessory profile (category 32)
- Apple TV target registration and persistent Target Control configuration
- Active-target selection and HomeKit button events
- HomeKit Data Stream setup and encrypted TCP transport
- `targetControl/whoami` target-to-HDS association
- Siri `dataSend/open`, Opus audio packets, acknowledgements and close handling
- 16 kHz / mono / signed PCM16 -> 20 ms Opus frames
- Async Python API
- Optional HTTP control API
- Persistent HAP pairing and target state

## Requirements

- Python 3.11+
- Apple TV / tvOS with HomeKit Target Control support (tvOS 12+)
- Host on the same LAN as the Apple TV
- mDNS/Bonjour traffic must reach the Apple TV

`opuslib-next-bundled` is used so supported CPython platforms do not need a separately installed `libopus` package.

## Install

```bash
python -m pip install -e .
```

Or directly from Git:

```bash
python -m pip install "git+https://github.com/jstnjx/atv-homekit-remote.git"
```

## Fastest way to run it

```bash
atv-homekit-remote
```

| Setting | Default | Environment variable |
|---|---:|---|
| HomeKit name | `Apple TV HomeKit Remote` | `HAP_NAME` |
| HomeKit username | generated and persisted | `HAP_USERNAME` |
| Pair code | generated and persisted | `HAP_PINCODE` / `PINCODE` |
| HAP port | `47129` | `HAP_PORT` |
| Control API bind | `127.0.0.1` | `CTRL_BIND` |
| Control API port | `8477` | `CTRL_PORT` |
| Persistent state directory | `.atv-homekit-remote` | `HAP_STORAGE` |

Then open **Apple Home -> Add Accessory -> More options**, select **Apple TV HomeKit Remote**, and enter the displayed setup code.

> Keep the state directory. It contains the HomeKit pairing keys and registered Target Control configuration.

## Python library API

### Start the accessory

```python
import asyncio

from atv_homekit_remote import AppleTVHomeKitRemote, RemoteConfig


async def main():
    remote = AppleTVHomeKitRemote(
        RemoteConfig(
            name="Apple TV HomeKit Remote",
            state_dir="./atv-homekit-remote-state",
        )
    )

    async with remote:
        print(remote.state)
        await asyncio.Event().wait()


asyncio.run(main())
```

Constructing the object does not bind sockets. `await remote.start()` / `async with remote` builds the HAP accessory on the currently running asyncio event loop.

### Buttons

After the Apple TV has registered as a target and activated the Target Control service:

```python
from atv_homekit_remote import Button

remote.set_active_identifier(207551296)
await remote.press(Button.MENU)
await remote.press("PLAY_PAUSE")
await remote.press(Button.ARROW_RIGHT, hold_ms=500)
```

`SIRI` is intentionally not exposed as an ordinary button press because the Siri button is coupled to an HDS audio session. Use the Siri API below.

### Send a complete PCM buffer to Siri

Input format is exactly:

- signed PCM16 little-endian
- 16,000 Hz
- mono

```python
pcm = open("utterance.pcm", "rb").read()
await remote.send_pcm(pcm, target=207551296)
```

A byte buffer is paced as real-time 20 ms audio by default. Pass `realtime=False` only when you explicitly want to push frames as fast as the HDS connection accepts them.

### Stream live microphone/TTS audio

```python
session = await remote.start_siri(target=207551296)
try:
    while True:
        chunk = await next_pcm_chunk()  # any chunk size is fine
        if chunk is None:
            break
        await session.write(chunk)
    await session.finish()
except Exception:
    await session.cancel()
    raise
```

The session buffers arbitrary chunk sizes into 640-byte / 20 ms PCM frames, encodes them with Opus, groups up to five Opus frames per HDS `dataSend/data` event, and sends the final `endOfStream` marker before waiting for the Apple TV acknowledgement.

You can also pass an async iterator directly:

```python
await remote.send_pcm(microphone_chunks(), target=207551296)
```

Async iterators are assumed to be live and are not artificially paced.

## Runtime state

```python
print(remote.state)
```

Example shape:

```json
{
  "name": "Apple TV HomeKit Remote",
  "active_identifier": 207551296,
  "active": true,
  "configured_targets": [
    {
      "target_identifier": 207551296,
      "target_name": "Living Room",
      "target_category": 24,
      "buttons": {}
    }
  ],
  "hds_targets": [207551296],
  "siri_ready": true
}
```

`siri_ready` requires all of the following: a selected target, the Apple TV having set Target Control `Active`, and a live HDS connection identified with `targetControl/whoami`.

## HTTP control API

The `atv-homekit-remote` command starts a local control API on `127.0.0.1:8477` unless `--no-http` is used.

| Endpoint | Purpose |
|---|---|
| `GET /state` | Current targets / active target / HDS readiness |
| `POST /active/<id>` | Select target |
| `POST /press/<BUTTON>?target=<id>` | Press and release a remote button |
| `POST /siri/stream?target=<id>` | Stream raw PCM16 16 kHz mono request body to Siri |
| `POST /siri/file?file=/path/test.wav&target=<id>` | Send a 16 kHz mono PCM16 WAV file |
| `POST /recover` | Toggle the Target Control Siri hardware capability and bump HAP config version |

Example:

```bash
curl -X POST --data-binary @utterance.pcm \
  "http://127.0.0.1:8477/siri/stream?target=207551296"
```

The control API deliberately defaults to loopback. If you expose it with `CTRL_BIND=0.0.0.0`, protect it at the host firewall or reverse proxy; the API does not add its own authentication unless `CTRL_TOKEN` is configured.

## HDS implementation notes

HDS session keys are derived from the **same Pair Verify shared secret as the originating HAP connection**:

- accessory -> controller: HKDF-SHA512 info `HDS-Read-Encryption-Key`
- controller -> accessory: HKDF-SHA512 info `HDS-Write-Encryption-Key`
- salt: `controllerKeySalt || accessoryKeySalt`
- transport: ChaCha20-Poly1305
- frame nonce: 64-bit little-endian counter, left-padded to the 96-bit nonce required by modern crypto APIs

The package includes an HDS serializer/deserializer supporting the scalar, string/data, list, dictionary and compression forms used by Apple TV Target Control / Siri sessions.

## Recovery behavior

The original Node bridge can republish a buttons-only accessory and then republish Siri services to encourage tvOS to reopen a stale HDS connection. `recover_hds()` implements the same capability-transition idea without destroying the HAP-python process: it toggles the Target Control `hardwareImplemented` capability and increments the HAP configuration version on both transitions.

```python
await remote.recover_hds()
```

If an Apple TV remains stuck after that, restart the process while preserving the state directory. Pairing and target configuration are retained.

## Tests

```bash
python -m pip install -e ".[test]"
pytest
```

The automated tests cover TLV8, HDS serialization, framing crypto primitives, target/audio capability encoding, and Opus frame generation. A real Apple TV is still required for an end-to-end HomeKit pairing/HDS/Siri validation.

## License / attribution

Apache-2.0. See `NOTICE` for attribution to the original `appletv-siri-voice` project and HAP-NodeJS, whose documented/open-source Target Control and HDS behavior was used as the interoperability reference for this Python implementation.
