import asyncio

from atv_homekit_remote import AppleTVHomeKitRemote, Button, RemoteConfig


async def main() -> None:
    remote = AppleTVHomeKitRemote(
        RemoteConfig(name="Apple TV HomeKit Remote", state_dir="./atv-homekit-remote-state")
    )
    async with remote:
        print("Pair with Apple Home if needed. Current state:", remote.state)
        while not remote.siri_ready:
            await asyncio.sleep(1)

        await remote.press(Button.TV_HOME)
        # pcm = open("utterance.pcm", "rb").read()
        # await remote.send_pcm(pcm)


if __name__ == "__main__":
    asyncio.run(main())
