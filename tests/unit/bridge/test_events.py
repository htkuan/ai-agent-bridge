import pytest

from agent_bridge.events import Completion, Usage


def test_usage_total_tokens_is_real_total():
    u = Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_creation_tokens=40)
    assert u.total_tokens == 100


def test_usage_add_accumulates_every_field():
    a = Usage(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_creation_tokens=4,
        num_turns=1,
        duration_api_ms=5,
        duration_ms=6,
        cost_usd=0.1,
    )
    b = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_creation_tokens=40,
        num_turns=2,
        duration_api_ms=50,
        duration_ms=60,
        cost_usd=0.2,
    )
    c = a + b
    assert c.input_tokens == 11
    assert c.output_tokens == 22
    assert c.cache_read_tokens == 33
    assert c.num_turns == 3
    assert c.duration_ms == 66
    assert c.cost_usd == pytest.approx(0.3)


def test_usage_from_completion_reads_metadata_and_fields():
    completion = Completion(
        text="hi",
        cost_usd=0.05,
        duration_ms=3000,
        metadata={
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_tokens": 300,
                "cache_creation_tokens": 50,
                "num_turns": 4,
                "duration_api_ms": 2500,
            }
        },
    )
    u = Usage.from_completion(completion)
    assert u is not None
    assert u.input_tokens == 100
    assert u.cost_usd == 0.05
    assert u.duration_ms == 3000
    assert u.duration_api_ms == 2500


def test_usage_from_completion_none_when_no_usage():
    assert Usage.from_completion(Completion(text="hi")) is None
