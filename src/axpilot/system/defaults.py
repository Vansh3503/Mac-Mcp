"""Read and write macOS preferences with the ``defaults`` command.

Domains and keys are validated before anything runs: they must be non-empty, free of NUL and
line breaks, and must not start with ``-`` (``defaults`` would read them as options). The global
domain can be named ``NSGlobalDomain``, ``-g`` or ``-globalDomain``; all three are passed on as
``NSGlobalDomain``. Values are written with an explicit type flag (``-bool``, ``-int``,
``-float`` or ``-string``), inferred from the Python type unless ``type_`` is given.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandRunner, ensure_success, excerpt

__all__ = [
    "GLOBAL_DOMAIN",
    "DefaultsType",
    "DefaultsValue",
    "defaults_read",
    "defaults_write",
    "defaults_write_argv",
    "encode_value",
]

DefaultsType = Literal["string", "int", "float", "bool"]
DefaultsValue = str | int | float | bool

GLOBAL_DOMAIN = "NSGlobalDomain"
DEFAULTS_TIMEOUT_S = 15.0
MAX_NAME_CHARS = 1024
_READ_MAX_OUTPUT = 500_000
_GLOBAL_ALIASES = frozenset({"-g", "-globalDomain", GLOBAL_DOMAIN})
_TYPE_ALIASES: dict[str, DefaultsType] = {
    "string": "string",
    "str": "string",
    "int": "int",
    "integer": "int",
    "float": "float",
    "real": "float",
    "bool": "bool",
    "boolean": "bool",
}
_TRUE_WORDS = frozenset({"true", "yes", "1", "on"})
_FALSE_WORDS = frozenset({"false", "no", "0", "off"})


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _validate_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"The preference {what} is empty.")
    if len(value) > MAX_NAME_CHARS:
        raise _invalid(f"The preference {what} is too long (max {MAX_NAME_CHARS} characters).")
    if any(ch in value for ch in "\x00\n\r"):
        raise _invalid(f"The preference {what} cannot contain NUL or line-break characters.")
    if value.startswith("-"):
        raise _invalid(
            f"The preference {what} cannot start with '-' (defaults would read it as an option): {value!r}.",
            hint="Use NSGlobalDomain for the global domain.",
        )
    return value


def _validate_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise _invalid("The preference domain must be text.")
    name = domain.strip()  # strip *before* validating, so " -flag" cannot become an option
    if name in _GLOBAL_ALIASES:
        return GLOBAL_DOMAIN
    _validate_name(name, "domain")
    if name.startswith("~"):
        name = str(Path(name).expanduser())  # a plist path such as ~/Library/Preferences/x.plist
    return name


def _parse_bool(value: DefaultsValue) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise _invalid(f"Cannot write {value!r} as a boolean; use true or false.")


def _parse_int(value: DefaultsValue) -> int:
    if isinstance(value, bool):
        raise _invalid("Cannot write a boolean as an integer; pass the number, or type bool.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise _invalid(f"Cannot write {value!r} as an integer.") from exc
    raise _invalid(f"Cannot write {value!r} as an integer.")


def _parse_float(value: DefaultsValue) -> float:
    if isinstance(value, bool):
        raise _invalid("Cannot write a boolean as a float; pass the number, or type bool.")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except ValueError as exc:
        raise _invalid(f"Cannot write {value!r} as a float.") from exc
    if not math.isfinite(number):
        raise _invalid("Preferences cannot store infinite or NaN floats.")
    return number


def _infer_type(value: DefaultsValue) -> DefaultsType:
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    raise _invalid(
        f"Unsupported preference value type {type(value).__name__}.",
        hint="Write a string, integer, float or boolean.",
    )


def encode_value(value: DefaultsValue, type_: str | None = None) -> tuple[str, str]:
    """``(flag, text)`` for writing ``value``: e.g. ``(-bool, "true")`` or ``(-int, "42")``.

    ``type_`` (``string``, ``int``, ``float``, ``bool`` or the aliases ``str``, ``integer``,
    ``real``, ``boolean``) forces a type and converts compatible values (``"yes"`` → ``true``).
    """
    if type_ is None:
        kind = _infer_type(value)
    else:
        resolved = _TYPE_ALIASES.get(type_.strip().lower()) if isinstance(type_, str) else None
        if resolved is None:
            raise _invalid(f"Unknown preference type {type_!r}; use string, int, float or bool.")
        kind = resolved
    if kind == "bool":
        return "-bool", "true" if _parse_bool(value) else "false"
    if kind == "int":
        return "-int", str(_parse_int(value))
    if kind == "float":
        return "-float", repr(_parse_float(value))
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise _invalid(f"Cannot write {value!r} as a string; pass text.")
    text = value if isinstance(value, str) else repr(value)
    if "\x00" in text:
        raise _invalid("Preference strings cannot contain NUL characters.")
    return "-string", text


def defaults_write_argv(domain: str, key: str, value: DefaultsValue, type_: str | None = None) -> list[str]:
    """``defaults write <domain> <key> -<type> <value>`` with every part validated."""
    flag, text = encode_value(value, type_)
    return ["defaults", "write", _validate_domain(domain), _validate_name(key, "key"), flag, text]


def defaults_read(runner: CommandRunner, domain: str, key: str | None = None) -> str:
    """The value of ``key`` in ``domain`` (or the whole domain), as ``defaults read`` prints it.

    Raises ``COMMAND_FAILED`` with ``details["reason"] == "not_found"`` when the domain or key
    does not exist.
    """
    dom = _validate_domain(domain)
    argv = ["defaults", "read", dom]
    if key is not None:
        argv.append(_validate_name(key, "key"))
    result = runner.run(argv, timeout_s=DEFAULTS_TIMEOUT_S, max_output=_READ_MAX_OUTPUT)
    if not result.ok and not result.timed_out and "does not exist" in result.stderr:
        target = f"key {key!r} in domain {dom!r}" if key is not None else f"domain {dom!r}"
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"The preference {target} does not exist.",
            hint="Check the spelling, or read the whole domain to see its keys.",
            details={"reason": "not_found", "exit_code": result.exit_code, "stderr": excerpt(result.stderr)},
        )
    ensure_success(result, what=f"Reading preferences of {dom}", timeout_s=DEFAULTS_TIMEOUT_S)
    return result.stdout.removesuffix("\n")


def defaults_write(
    runner: CommandRunner,
    domain: str,
    key: str,
    value: DefaultsValue,
    type_: str | None = None,
) -> None:
    """Write ``value`` to ``key`` in ``domain`` (bool→``-bool``, int→``-int``, float→``-float``,
    str→``-string`` unless ``type_`` says otherwise). Raises ``INVALID_ARGUMENT`` for bad input
    and ``COMMAND_FAILED`` if ``defaults`` fails. Apps may need a restart to see the change.
    """
    argv = defaults_write_argv(domain, key, value, type_)
    result = runner.run(argv, timeout_s=DEFAULTS_TIMEOUT_S)
    ensure_success(
        result,
        what=f"Writing preference {argv[3]!r} in {argv[2]}",
        timeout_s=DEFAULTS_TIMEOUT_S,
        hint="Sandboxed apps keep preferences in their container; check the domain name.",
    )
