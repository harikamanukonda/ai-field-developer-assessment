import pytest
from assessment.guardrail import StreamingRedactor


@pytest.mark.parametrize("value", ["alice@example.com", "123-45-6789", "4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111"])
def test_every_split_boundary(value):
    for split in range(len(value) + 1):
        redactor = StreamingRedactor()
        output = redactor.feed("Before; " + value[:split])
        output += redactor.feed(value[split:] + "; after") + redactor.finish()
        assert output == "Before; [REDACTED]; after"


def test_single_character_chunks_and_plain_text():
    value = "Hello, alice@example.com; SSN 123-45-6789; good!"
    redactor = StreamingRedactor()
    output = "".join(redactor.feed(c) for c in value) + redactor.finish()
    assert output == "Hello, [REDACTED]; SSN [REDACTED]; good!"


def test_bounded_memory_and_fail_closed_overlong_candidates():
    redactor = StreamingRedactor(max_candidate=32)
    output = "".join(redactor.feed("a" * 10) for _ in range(1000))
    assert len(redactor.pending) <= 32
    output += redactor.feed("@example.com; safe!") + redactor.finish()
    assert output == "[REDACTED]; safe!"
