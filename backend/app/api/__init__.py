"""HTTP layer for the converter.

``routes.py`` holds the ``/api`` router and nothing else of substance: it parses
requests, sets status codes and streams bytes, while every decision about what a
failure *means* belongs to ``app.services.converter``.

Nothing is imported here eagerly, so ``import app.api`` does not drag in fastapi
(``app.main`` imports the router explicitly).
"""
