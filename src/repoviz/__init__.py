"""repoviz: a read-only repository architecture, change, and activity visualizer.

The package is organised in layers:

* ``sources`` / ``gitutil`` read repository content at any revision without
  touching the working tree or the index.
* ``discovery`` inspects a tree and builds a :class:`~repoviz.discovery.RepositoryProfile`.
* ``analyzers`` are plugins that turn a tree into the normalized graph model
  defined in ``model``.
* ``pipeline`` runs the analyzers, ``diff`` compares snapshots, ``activity``
  tracks in-progress work and ``flow`` computes affected call flow.
* ``render`` turns the model into Mermaid text and HTML; ``server`` exposes a
  small local API for the live web application.
"""

__version__ = "0.1.0"

#: Version of the JSON data model emitted by snapshots, diffs and reports.
SCHEMA_VERSION = 1
