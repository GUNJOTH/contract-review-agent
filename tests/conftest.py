"""Deterministic test-only environment; never used by the production process."""

import os

# The application deliberately fails closed when API_TOKEN is absent. Keep the
# integration suite self-contained without reading a developer's real token.
os.environ["API_TOKEN"] = "test-api-token"
os.environ["AUTH_HEADER_NAME"] = "X-API-Token"
