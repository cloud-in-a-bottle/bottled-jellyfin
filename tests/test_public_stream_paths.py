"""Jellyfin clients may use OpenAPI casing or jellyfin-web's lowercase media paths."""

import tomllib
from pathlib import Path
from types import SimpleNamespace

from compute_space.core.apps import is_public_path


def test_both_client_stream_url_casings_are_public() -> None:
    manifest = tomllib.loads((Path(__file__).resolve().parent.parent / "cloudinabottle.toml").read_text())
    app = SimpleNamespace(public_paths=manifest["routing"]["public_paths"])

    for path in (
        "/Videos/abc/stream.mp4",  # Jellyfin's documented OpenAPI route
        "/videos/abc/stream.mp4",  # jellyfin-web's URL
        "/Audio/abc/stream.mp3",
        "/audio/abc/stream.mp3",
    ):
        assert is_public_path(app, path), path
    # Exposing streams must not expose Jellyfin's owner-only library settings.
    assert not is_public_path(app, "/Library/VirtualFolders")
