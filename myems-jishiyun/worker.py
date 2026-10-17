"""Compatibility entry point for the installed acquisition package."""

from myems_jishiyun.worker import main


if __name__ == "__main__":
    raise SystemExit(main())
