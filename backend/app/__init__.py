"""Application package for the image-to-pptx converter.

The conversion core lives here and is import-safe in any host: the modules use
RELATIVE imports between each other (``from . import config``,
``from .schemas import SlideSpec``), so the same code backs the CLI
(``backend/cli.py``), the HTTP API (``app.api``) and the service layer
(``app.services``) with no ``sys.path`` manipulation anywhere.

Nothing is imported eagerly here on purpose: ``import app`` must stay free of
pydantic / python-pptx / openai so that a caller can pull in exactly the
submodule it needs (``from app import config``) without paying for the rest.
"""
