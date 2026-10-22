"""Creation flags for Windows background commands, without console flashes."""
import os
import subprocess


def hidden() -> dict:
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
