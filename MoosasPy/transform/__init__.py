"""Geometry-source transformation public API."""

__all__ = [
    "TransformOptions",
    "transform",
]


def __getattr__(name):
    if name == "TransformOptions":
        from .options import TransformOptions

        return TransformOptions
    if name == "transform":
        from . import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
