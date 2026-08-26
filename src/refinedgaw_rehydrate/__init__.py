"""Download and rehydrate source documents indexed by RefinedGAW."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("refinedgaw-rehydrate")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
