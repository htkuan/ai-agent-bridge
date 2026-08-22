"""Shared, typed test doubles for the bridge's protocol seams.

Every fake implements the corresponding interface from
``agent_bridge.bridge.protocols`` (or, for external boundaries, the API subset the
adapter actually uses) and records its calls so tests can assert on them.
The contract suites in ``tests/contracts`` pin the fakes to the real
implementations' behaviour.
"""

from tests.fakes.agents import ControllerCall, FakeAgentController
from tests.fakes.bridge import FakeBridge, RouterCall
from tests.fakes.claude_cli import FakeClaudeCLI
from tests.fakes.codex_cli import FakeCodexCLI
from tests.fakes.pi_cli import FakePiCLI
from tests.fakes.platforms import FakePlatformAdapter
from tests.fakes.slack import (
    FakeBoltApp,
    FakeSlackClient,
    SlackCall,
    dm_event,
    mention_event,
)

__all__ = [
    "ControllerCall",
    "FakeAgentController",
    "FakeBoltApp",
    "FakeBridge",
    "FakeClaudeCLI",
    "FakeCodexCLI",
    "FakePiCLI",
    "FakePlatformAdapter",
    "FakeSlackClient",
    "RouterCall",
    "SlackCall",
    "dm_event",
    "mention_event",
]
