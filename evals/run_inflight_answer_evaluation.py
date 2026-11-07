"""Run only explicitly authorized live acceptance of double-text final answers."""
from evals.inflight_runner import main

if __name__=='__main__':
    raise SystemExit(main())
