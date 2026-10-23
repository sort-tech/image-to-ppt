"""Suite-wide setup.

``app.config`` picks its provider from the environment at import time, and a
developer's shell often already exports ``GEMINI_API_KEY``, ``OPENAI_API_KEY``
or ``ANTHROPIC_API_KEY`` for other tools. Left in place, that would silently switch the whole suite from
the proxy to a direct provider. Dropped here, before any test module imports
``config``; tests that exercise those keys set them explicitly.
"""

import os

for _name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_name, None)
