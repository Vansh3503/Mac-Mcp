"""``axpilot.system.keychain``: generic passwords through ``/usr/bin/security`` (a FakeRunner
plays ``security`` here; the real tool only runs on a Mac)."""

from __future__ import annotations

import shlex

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.keychain import ITEM_NOT_FOUND, SECURITY, Keychain
from axpilot.system.runner import CommandResult, FakeCall, FakeRunner

SERVICE, ACCOUNT = "io.example.service", "me"
SECRET = "s3cret-Token_value.1234567890abcdef"


class FakeSecurity:
    """A tiny ``security``: find/delete-generic-password arguments, and ``-i`` with
    add-generic-password on stdin. ``items`` is the keychain; ``drop_writes`` loses writes."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.drop_writes = False
        self.runner = FakeRunner()
        self.runner.register([SECURITY], handler=self.handle)

    def handle(self, call: FakeCall) -> CommandResult:
        argv = call.argv
        if argv[1] == "-i":
            words = shlex.split(call.input_text or "")
            assert words[:2] == ["add-generic-password", "-U"]
            options = dict(zip(words[2::2], words[3::2], strict=True))
            if not self.drop_writes:
                self.items[(options["-s"], options["-a"])] = options["-w"]
            return CommandResult(argv, 0, "", "")
        options = dict(zip(argv[2::2], argv[3::2], strict=False))
        key = (options["-s"], options["-a"])
        if key not in self.items:
            return CommandResult(argv, ITEM_NOT_FOUND, "", "The specified item could not be found.")
        if argv[1] == "delete-generic-password":
            del self.items[key]
            return CommandResult(argv, 0, "", "")
        return CommandResult(argv, 0, self.items[key] + "\n", "")


@pytest.fixture
def security() -> FakeSecurity:
    return FakeSecurity()


def test_write_then_read(security: FakeSecurity) -> None:
    chain = Keychain(security.runner)
    assert chain.read(SERVICE, ACCOUNT) is None
    chain.write(SERVICE, ACCOUNT, SECRET)
    assert chain.read(SERVICE, ACCOUNT) == SECRET
    assert security.items == {(SERVICE, ACCOUNT): SECRET}


def test_the_secret_never_appears_in_arguments(security: FakeSecurity) -> None:
    Keychain(security.runner).write(SERVICE, ACCOUNT, SECRET)
    for call in security.runner.calls:
        assert SECRET not in " ".join(call.argv)
    (write,) = security.runner.calls_for([SECURITY, "-i"])
    assert write.input_text is not None
    assert f"-w {SECRET}" in write.input_text


def test_delete(security: FakeSecurity) -> None:
    chain = Keychain(security.runner)
    chain.write(SERVICE, ACCOUNT, SECRET)
    assert chain.delete(SERVICE, ACCOUNT) is True
    assert chain.delete(SERVICE, ACCOUNT) is False


def test_a_write_that_does_not_stick_is_an_error(security: FakeSecurity) -> None:
    security.drop_writes = True  # `security -i` said nothing, but the item is not there
    with pytest.raises(AXPilotError) as info:
        Keychain(security.runner).write(SERVICE, ACCOUNT, SECRET)
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert SECRET not in str(info.value.to_dict())


@pytest.mark.parametrize(
    ("service", "account", "secret"),
    [("has space", ACCOUNT, SECRET), (SERVICE, 'quo"te', SECRET), (SERVICE, ACCOUNT, "two words"),
     (SERVICE, ACCOUNT, "line\nbreak"), (SERVICE, ACCOUNT, ""), (SERVICE, ACCOUNT, "x" * 513)],
)  # fmt: skip
def test_values_security_could_misread_are_refused(
    security: FakeSecurity, service: str, account: str, secret: str
) -> None:
    with pytest.raises(AXPilotError) as info:
        Keychain(security.runner).write(service, account, secret)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert security.runner.calls == []


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (CommandResult([SECURITY], 51, "", "User interaction is not allowed."), ErrorCode.COMMAND_FAILED),
        (CommandResult([SECURITY], -1, "", "", timed_out=True), ErrorCode.TIMEOUT),
        (CommandResult([SECURITY], 127, "", "not found"), ErrorCode.NOT_SUPPORTED),
    ],
)
def test_failures_never_include_the_output(result: CommandResult, code: ErrorCode) -> None:
    def answer(call: FakeCall) -> CommandResult:
        return CommandResult(call.argv, result.exit_code, SECRET, SECRET, result.timed_out)

    runner = FakeRunner()
    runner.register([SECURITY], handler=answer)
    chain = Keychain(runner)
    for operation in (lambda: chain.read(SERVICE, ACCOUNT), lambda: chain.write(SERVICE, ACCOUNT, SECRET),
                      lambda: chain.delete(SERVICE, ACCOUNT)):  # fmt: skip
        with pytest.raises(AXPilotError) as info:
            operation()
        assert info.value.code is code
        assert SECRET not in str(info.value.to_dict()) + info.value.message


def test_an_empty_password_reads_as_none() -> None:
    runner = FakeRunner()
    runner.register([SECURITY, "find-generic-password"], stdout="\n")
    assert Keychain(runner).read(SERVICE, ACCOUNT) is None
