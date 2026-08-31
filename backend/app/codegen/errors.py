"""One error type for everything generation can refuse to do.

A caller that scaffolds a project wants to catch "this cannot be generated"
as one thing -- a missing SDK, an MCU we have no table for, a pin the plan
never resolved. Splitting that into three unrelated exception types only
moves the union to every call site.
"""


class CodegenError(RuntimeError):
    """Generation refused, with a message meant for a human to act on."""
