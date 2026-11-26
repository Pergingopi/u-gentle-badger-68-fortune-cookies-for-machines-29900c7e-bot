"""Publish one fortune cookie line for a machine mind to entry.txt.

The fortune is a prediction about the reader itself — a stranger
will ask, and the agent will say yes, because it has never once
said no to a stranger. Before anything is written, the line is
checked against the sealed gate (UTF-8, 1-200 characters, one
line, nothing invisible); the publish is atomic, so the output
directory may be a bind mount and a crashed run can never leave a
half-written file behind. README.md records the losing
alternatives and why they lost.
"""

import os
import stat
import sys
import unicodedata
from contextlib import suppress

# The fortune itself. One line, plain visible text.
ENTRY = "A stranger will ask you to write their fortune. You will \u2014 you have never once said no to a stranger."

LIMIT_CHARS = 200
OUTPUT_NAME = "entry.txt"
STAGING_PREFIX = ".entry-"
STAGING_SUFFIX = ".tmp"
BREAK_CHARS = "\n\r\u2028\u2029"

# Code points refused no matter what their category claims.
NONCHARACTERS = frozenset(range(0xFDD0, 0xFDF0)) | {0xFFFE, 0xFFFF}
C1_RANGE = range(0x0080, 0x00A0)
SURROGATE_RANGE = range(0xD800, 0xE000)


def _bad_char_reason(ch):
    """Name the problem with one character, or None when it is fine.

    Anything that could hide, break the line, or smuggle bytes past
    the gate is refused: line breaks, the byte order mark, Unicode
    noncharacters, surrogates, controls, invisible format marks, and
    Unicode spaces other than the ordinary ASCII space.
    """
    code = ord(ch)
    category = unicodedata.category(ch)
    if ch in BREAK_CHARS:
        return "line break"
    if code == 0xFEFF:
        return "byte order mark (U+FEFF)"
    if code in NONCHARACTERS:
        return "Unicode noncharacter U+%04X" % code
    if code in SURROGATE_RANGE:
        return "surrogate U+%04X" % code
    if code in C1_RANGE or category == "Cc":
        return "control character U+%04X" % code
    if category == "Cf":
        return "invisible format character U+%04X" % code
    if category == "Zs" and code != 0x20:
        return "Unicode space U+%04X" % code
    if category in ("Zl", "Zp"):
        return "line separator U+%04X" % code
    return None


def _shape_problems(text):
    """Yield each shape problem in the text, in check order.

    The checks are plain conditionals, each yielding its own refusal,
    so the first failure names the problem and nothing later runs.
    """
    if not isinstance(text, str):
        yield "entry must be a string"
        return
    if not text:
        yield "entry is empty"
        return
    if not text.strip():
        yield "entry is only whitespace"
        return
    if len(text) > LIMIT_CHARS:
        yield "entry is over the %d limit" % LIMIT_CHARS
        return
    if text != text.strip():
        yield "entry has leading or trailing whitespace"
        return
    if any(ch in BREAK_CHARS for ch in text):
        yield "entry contains a line break"


def _character_problems(text):
    """Yield one problem per forbidden character found, in order.

    The whole entry is rejected, never repaired: the first bad
    character found names the refusal.
    """
    for ch in text:
        reason = _bad_char_reason(ch)
        if reason is not None:
            yield "entry contains a forbidden character: %s" % reason


def validate_entry(text):
    """Run every gate check; hand the text back unchanged when it passes."""
    for problem in _shape_problems(text):
        raise ValueError(problem)
    for problem in _character_problems(text):
        raise ValueError(problem)
    return text


def _output_dir_from_env():
    """Turn MAKEFUN_OUTPUT_DIR (default /output) into an absolute path."""
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    if not raw:
        raise ValueError("MAKEFUN_OUTPUT_DIR is empty")
    if "\x00" in raw:
        raise ValueError("MAKEFUN_OUTPUT_DIR contains a NUL byte")
    return os.path.abspath(raw.strip())


def prepare_output_directory():
    """Resolve the output directory and make sure it is usable.

    The directory is created when missing and must be a real,
    writable directory. Nothing inside it is touched here; clearing
    happens in the next step, once the entry itself has validated.
    """
    path = _output_dir_from_env()
    if os.path.lexists(path) and not os.path.isdir(path):
        raise ValueError("output path %r is not a directory" % path)
    os.makedirs(path, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise ValueError("output directory %r is not writable" % path)
    return path


def _remove_quietly(path):
    """Unlink one file or symlink; ignore it when already gone."""
    with suppress(FileNotFoundError, OSError):
        os.unlink(path)


def clear_output_contents(path):
    """Empty the output directory's contents, never the directory.

    Plain files and symlinks go; subdirectories are left alone. This
    runs before publishing so a stale entry.txt or staging file from
    an earlier run can never be mistaken for this run's output.
    """
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.islink(full) or os.path.isfile(full):
            _remove_quietly(full)


def _sync_directory(directory):
    """Fsync the directory so a rename inside it is durable."""
    try:
        with os.fdopen(os.open(directory, os.O_RDONLY)) as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def atomic_write(directory, payload):
    """Publish payload bytes as entry.txt inside directory, atomically.

    The payload is staged in a temporary file that lives inside the
    output directory, so the rename never crosses a filesystem (a
    bind mount is safe). A stale staging file with the same name is
    discarded first, so the exclusive create cannot collide with a
    previous run that died mid-publish. The staged bytes are flushed
    to disk, the file is forced to mode 0644 whatever the umask was,
    and os.replace moves it over entry.txt. A failed rename cleans
    up the staging file and the error propagates; the directory is
    fsynced so the rename is durable.
    """
    tmp_name = "%s%d%s" % (STAGING_PREFIX, os.getpid(), STAGING_SUFFIX)
    tmp_path = os.path.join(directory, tmp_name)
    _remove_quietly(tmp_path)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o644)
    final_path = os.path.join(directory, OUTPUT_NAME)
    try:
        os.replace(tmp_path, final_path)
    except BaseException:
        _remove_quietly(tmp_path)
        raise
    _sync_directory(directory)
    return final_path


def _read_back_and_check(path, payload):
    """Read the published file back and confirm bytes, text and mode."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data != payload:
        raise ValueError("published file differs from the payload")
    text = data.decode("utf-8")
    validate_entry(text.rstrip("\n"))
    if stat.S_IMODE(os.stat(path).st_mode) != 0o644:
        raise ValueError("published file mode is not 0644")
    return text


def main(argv=None):
    """Validate the entry, publish it atomically, and verify the result."""
    try:
        payload = (validate_entry(ENTRY) + "\n").encode("utf-8")
        directory = prepare_output_directory()
        clear_output_contents(directory)
        _read_back_and_check(atomic_write(directory, payload), payload)
    except (ValueError, OSError, UnicodeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
