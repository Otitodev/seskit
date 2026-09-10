"""Long polling has to be allowed to wait (§15).

A deployment logged a stack trace a minute, for ever. `sqs:ReceiveMessage` was
timing out on every pass, delivery events never arrived, and the message said
only that SES "did not complete sqs:ReceiveMessage".

The cause was arithmetic. One shared botocore config carried
`read_timeout=15`, written for the request path where failing fast is the
point. SQS long polling holds the connection open until a message arrives or
the wait expires - twenty seconds by default, and on an idle queue always the
full twenty. Fifteen is less than twenty, so an empty receive could not
succeed. `max_attempts=3` then retried it twice, which is why each pass took
about a minute.

Nothing caught it because nothing had both halves: the timeout lives in the
provider package and the wait comes from settings, and no test put a real
long poll against a real config. These do, in the only way that does not need
AWS - by checking the numbers agree.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.config import Config
from seskit_core.config import Settings
from seskit_provider_aws_ses.client import BOTO_CONFIG, polling_config
from seskit_provider_aws_ses.sqs import MAX_WAIT_SECONDS


def _total_attempts(config: Config) -> int:
    """How many attempts a config allows in total, however botocore has spelt it.

    botocore rewrites `retries` **in place** the first time a client is built
    from a config, and not only by renaming: `{"max_attempts": 3}` becomes
    `{"total_max_attempts": 4}`, because `max_attempts` counts retries and the
    total counts the first try as well.

    `BOTO_CONFIG` is a module-level object shared by every AWS call, so in a
    full suite another test has usually built a client from it before this one
    runs, while running this file alone leaves it untouched. Comparing the raw
    dicts therefore passes locally and fails in CI - and comparing whichever key
    happens to exist compares 3 against 4, which fails differently.

    Normalising to one meaning is the only comparison that holds either way.
    """
    retries = _option(config, "retries") or {}
    if "total_max_attempts" in retries:
        return int(retries["total_max_attempts"])
    return int(retries["max_attempts"]) + 1


def _option(config: Config, name: str) -> Any:
    """Read one botocore config option.

    Through `getattr` because botocore builds these attributes at runtime from
    its own option list, so the type stubs do not declare them and a direct
    `config.read_timeout` fails type checking while working perfectly.
    """
    return getattr(config, name)


def test_a_long_poll_may_wait_longer_than_it_is_given() -> None:
    """The bug, stated as arithmetic.

    Whatever the wait, the read timeout has to exceed it - otherwise the call
    is guaranteed to time out on an empty queue, which is the ordinary case for
    a project that is not sending at that moment.
    """
    for wait in range(0, MAX_WAIT_SECONDS + 1):
        timeout = _option(polling_config(wait), "read_timeout")
        assert timeout > wait, f"a {wait}s long poll against a {timeout}s read timeout"


def test_the_default_wait_would_have_failed_on_the_request_config() -> None:
    """Naming the actual failure, so this test explains itself if it ever goes
    red for a different reason.
    """
    wait = min(Settings.model_fields["EVENT_POLL_WAIT_SECONDS"].default, MAX_WAIT_SECONDS)

    assert _option(BOTO_CONFIG, "read_timeout") <= wait, (
        "the request-path timeout no longer undercuts the long poll - if that "
        "is deliberate, this test has served its purpose and can go"
    )
    assert _option(polling_config(wait), "read_timeout") > wait


def test_the_request_path_keeps_its_short_timeout() -> None:
    """The fix must not slow down the calls that render a page. A dashboard
    waiting thirty seconds on an unreachable endpoint is the failure the short
    timeout exists to prevent.
    """
    assert _option(BOTO_CONFIG, "read_timeout") == 15


@pytest.mark.parametrize("wait", [0, 1, 5, 20])
def test_polling_keeps_the_retry_and_connect_behaviour(wait: int) -> None:
    """Only the read timeout differs. A separate config is a place for the two
    to drift, so what is not deliberately different should stay identical.
    """
    config = polling_config(wait)

    assert _option(config, "connect_timeout") == _option(BOTO_CONFIG, "connect_timeout")
    assert _total_attempts(config) == _total_attempts(BOTO_CONFIG)


def test_a_receive_asks_for_the_wait_its_client_was_built_for() -> None:
    """The clamp to twenty is applied once, and both the client and the call
    see the same number.

    Clamping in one place and not the other is how this comes back: a config
    built for the requested wait, against a call that asked for less, is the
    same mismatch pointing the other way.
    """
    import inspect

    from seskit_provider_aws_ses import sqs

    source = inspect.getsource(sqs.SQSNotificationQueue.receive)

    assert "wait = min(wait_seconds, MAX_WAIT_SECONDS)" in source
    assert "self._client(wait_seconds=wait)" in source
    assert "WaitTimeSeconds=wait," in source
