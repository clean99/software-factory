"""Subcommands contributed by the v4 modules. Each module exposes register(sub)."""
import importlib

MODULES = (
    "factory.core.receipts",
    "factory.core.checks",
    "factory.core.review",
    "factory.core.briefs",
    "factory.core.lessons",
    "factory.core.evals",
    "factory.core.nextaction",
    "factory.oss.lanes",
    "factory.oss.sync",
)


def register(sub):
    for name in MODULES:
        importlib.import_module(name).register(sub)
