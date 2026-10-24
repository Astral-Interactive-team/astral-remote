import pytest

from astral_remote.agent.network.config import parse_election_members
from astral_remote.agent.network.errors import PeerProtocolError
from astral_remote.agent.network.models import ElectionMember


def test_empty_election_membership_defaults_to_empty():
    assert parse_election_members(None) == ()
    assert parse_election_members({}) == ()


def test_parse_election_members():
    config = {
        "peerNetwork": {
            "election": {
                "members": [
                    {
                        "installationId": "install-a",
                        "nodeId": "node-a",
                    },
                    {
                        "installationId": "install-b",
                        "nodeId": "node-b",
                    },
                ]
            }
        }
    }

    assert parse_election_members(config) == (
        ElectionMember("install-a", "node-a"),
        ElectionMember("install-b", "node-b"),
    )


def test_election_member_ids_are_normalized():
    config = {
        "peerNetwork": {
            "election": {
                "members": [
                    {
                        "installationId": " install-a ",
                        "nodeId": " node-a ",
                    }
                ]
            }
        }
    }

    assert parse_election_members(config) == (
        ElectionMember("install-a", "node-a"),
    )


@pytest.mark.parametrize(
    "config",
    [
        {"peerNetwork": {"election": []}},
        {"peerNetwork": {"election": {"members": {}}}},
        {"peerNetwork": {"election": {"members": ["invalid"]}}},
        {
            "peerNetwork": {
                "election": {
                    "members": [{"nodeId": "node-a"}]
                }
            }
        },
        {
            "peerNetwork": {
                "election": {
                    "members": [
                        {
                            "installationId": "install-a",
                            "nodeId": " ",
                        }
                    ]
                }
            }
        },
        {
            "peerNetwork": {
                "election": {
                    "members": [
                        {
                            "installationId": "install-a",
                            "nodeId": "node-a",
                        },
                        {
                            "installationId": "install-a",
                            "nodeId": "node-b",
                        },
                    ]
                }
            }
        },
        {
            "peerNetwork": {
                "election": {
                    "members": [
                        {
                            "installationId": "install-a",
                            "nodeId": "node-a",
                        },
                        {
                            "installationId": "install-b",
                            "nodeId": "node-a",
                        },
                    ]
                }
            }
        },
    ],
)
def test_invalid_election_membership_is_rejected(config):
    with pytest.raises(PeerProtocolError):
        parse_election_members(config)