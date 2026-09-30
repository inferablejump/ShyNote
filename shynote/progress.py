"""Terminal-only transfer progress; machine-readable results stay on stdout."""
from contextlib import contextmanager, redirect_stderr
import sys


@contextmanager
def transfer_progress(description, enabled=None):
    stream = sys.stderr
    if enabled is None:
        enabled = stream.isatty()
    if not enabled:
        yield None
        return

    from tqdm import tqdm
    from tqdm.contrib import DummyTqdmFile

    bar = None

    def report(completed, total, name):
        nonlocal bar
        if bar is None:
            bar = tqdm(total=total, desc=description, file=stream, dynamic_ncols=True,
                       bar_format="{desc} {n_fmt}/{total_fmt} |{bar}|{postfix}", leave=False)
        # Invalid paths may contain terminal control characters before validation.
        label = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in name)
        bar.set_postfix_str(label, refresh=False)
        bar.n = completed
        bar.refresh()

    # Retry notices clear and redraw the bar instead of overwriting it.
    with redirect_stderr(DummyTqdmFile(stream)):
        try:
            yield report
        finally:
            if bar is not None:
                bar.close()
