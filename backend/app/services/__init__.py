"""Service layer: orchestration between the API and the conversion core.

``converter.py`` is the seam. It is framework-free by design - it imports
nothing from fastapi - so the whole pipeline (validate the upload, analyze it,
validate the spec, build the deck in memory) can be driven from a test, a
script or a different web framework, and the core modules keep exactly one
caller shape.

``preview.py`` is framework-free for the same reason, and core-free besides: it
only reads a built ``Presentation``, so it can be pointed at any deck.

Nothing is imported here eagerly: ``import app.services`` must not pull in
pydantic / python-pptx / openai.
"""
