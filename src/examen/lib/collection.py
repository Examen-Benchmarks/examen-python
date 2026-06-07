"""`Collection`: a bench-scoped, nestable organisational tree.

A collection is the structural layer between a bench and its experiments — the
`APIRouter` to the bench's `app` (see ``examen-docs/concepts/collections.md``).
It carries a stable `key`, a display `name`, and an optional `description`
(D15), owns its own ``@collection.experiment[In, Out](...)`` registrar, and can
nest other collections via ``.include(child)``.

Collections are organisational metadata only: they sit *above* the experiment
and do not participate in run identity or the iteration grouping key. At
``run()`` the bench walks every included subtree and emits each experiment's
``collection_path`` (root-down) in the ingest payload.
"""

from examen.lib.host import _ExperimentHost


class Collection(_ExperimentHost):
    """A nestable, bench-scoped grouping of experiments and child collections.

    Identified by its ``key`` among its siblings (the same `key` may be reused
    under a different parent). ``name`` is a display label; ``description`` is
    optional. Mount experiments with ``@collection.experiment[In, Out](...)`` and
    nest sub-collections with ``collection.include(child)``.
    """

    def __init__(self, key: str, name: str, description: str | None = None) -> None:
        super().__init__()
        self.key = key
        self.name = name
        self.description = description
