"""What cisco_roomos_xapi tells the installer about the codec user's role.

The RoomOS 11.27 API guide's "HTTP XMLAPI Authentication" paragraph says the
XMLAPI accepts only a user with the ADMIN role, so over HTTP a user with only
the Integrator role is refused on every request and the device goes offline
with a sign-in fault. The setup text and the Username field have to say Admin,
and must not tell anyone an Integrator user will do.

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "video" / "cisco_roomos_xapi.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))


def test_version_bumped():
    assert str(INFO["version"]) == "1.3.1"


def test_setup_asks_for_an_admin_user_and_says_integrator_is_refused():
    setup = " ".join(INFO["help"]["setup"].split())
    assert '"Admin" role' in setup
    assert "accepts only Admin users" in setup
    assert 'only the "Integrator" role is refused' in setup
    assert "still controls the codec" not in setup


def test_username_field_asks_for_the_admin_role():
    description = INFO["config_schema"]["username"]["description"]
    assert "Admin role" in description
    assert "Integrator" not in description
