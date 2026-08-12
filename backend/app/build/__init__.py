"""Compiling generated firmware (M4).

Three pieces, deliberately separate:

* `workspace` -- where a project's files live on disk, and the only code
  allowed to turn a model-supplied path into a real one.
* `client` -- HTTP to the isolated build container.
* `diagnostics` -- compiler output in, structured errors out.

The build container itself (`deploy/builder/`) holds no logic beyond running
`make`, so everything here is testable without a cross toolchain installed.
"""
