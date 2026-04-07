"""Output helper for cmlxc."""

import shutil
import subprocess
import sys
import textwrap
from contextlib import contextmanager

from termcolor import colored


class Out:
    """Colored, prefixed output printer with section formatting."""

    def __init__(self, prefix="", verbosity=0):
        self.prefix = prefix
        self.verbosity = verbosity
        self.sepchar = "━"
        self.section_width = shutil.get_terminal_size((80, 24)).columns

    def red(self, msg, file=sys.stderr):
        print(colored(self.prefix + msg, "red"), file=file, flush=True)

    def green(self, msg, file=sys.stderr):
        print(colored(self.prefix + msg, "green"), file=file, flush=True)

    def print(self, msg="", **kwargs):
        if msg:
            msg = self.prefix + msg
        print(msg, flush=True, **kwargs)

    __call__ = print

    def shell(self, cmd, quiet=False, **kwargs):
        cmd = _collapse(cmd)
        if not quiet:
            self.print(f"$ {cmd}")
        indent = self.prefix + "  "
        proc = subprocess.Popen(
            cmd,
            shell=True,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        for line in proc.stdout:
            sys.stdout.write(indent + line)
            sys.stdout.flush()
        ret = proc.wait()
        if ret:
            self.red(f"command failed with exit code {ret}: {cmd}")
        return ret

    def new_prefixed_out(self, newprefix="  "):
        return type(self)(
            prefix=self.prefix + newprefix,
            verbosity=self.verbosity,
        )

    def _format_header(self, title):
        width = self.section_width - len(self.prefix)
        bar = self.sepchar * (width - len(title) - 5)
        return f"{self.sepchar * 3} {title} {bar}"

    @contextmanager
    def section(self, title):
        self.green(self._format_header(title))
        yield

    def section_line(self, title):
        self.green(self._format_header(title))


def _collapse(text):
    return textwrap.dedent(text).replace("\n", " ").strip()
