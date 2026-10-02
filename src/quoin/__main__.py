import sys

MIN_PYTHON = (3, 10)

if sys.version_info[:2] < MIN_PYTHON:
    sys.stderr.write(
        "quoin requires Python >= %d.%d; this is %d.%d at %s\n"
        % (MIN_PYTHON + tuple(sys.version_info[:2]) + (sys.executable,))
    )
    raise SystemExit(1)

from quoin.cli import main  # noqa: E402

raise SystemExit(main())
