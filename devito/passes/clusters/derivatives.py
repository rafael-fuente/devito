from devito.finite_differences import IndexDerivative
from devito.ir import Backward, Forward, Interval, IterationSpace, Queue
from devito.passes.clusters.misc import fuse
from devito.symbolics import (retrieve_dimensions, reuse_if_untouched, q_leaf,
                              uxreplace)
from devito.tools import filter_ordered, timed_pass
from devito.types import Eq, Inc, StencilDimension, Symbol

__all__ = ['lower_index_derivatives']


@timed_pass()
def lower_index_derivatives(clusters, mode=None, **kwargs):
    clusters, weights, mapper = _lower_index_derivatives(clusters, **kwargs)

    if not weights:
        return clusters

    if mode != 'noop':
        clusters = fuse(clusters, toposort='maximal')

    # At this point we can detect redundancies induced by inner derivatives that
    # previously were just not detectable via e.g. plain CSE. For example, if
    # there were two IndexDerivatives such as `(p.dx + m.dx).dx` and `m.dx.dx`
    # then it's only after `_lower_index_derivatives` that they're detectable!
    clusters = CDE(mapper).process(clusters)

    return clusters


def _lower_index_derivatives(clusters, sregistry=None, **kwargs):
    weights = {}
    processed = []
    mapper = {}

    def dump(exprs, c):
        if exprs:
            processed.append(c.rebuild(exprs=exprs))
            exprs[:] = []

    for c in clusters:
        exprs = []
        for e in c.exprs:
            # Optimization 1: if the LHS is already a Symbol, then surely it's
            # usable as a temporary for one of the IndexDerivatives inside `e`
            if e.lhs.is_Symbol and e.operation is None:
                reusable = {e.lhs}
            else:
                reusable = set()

            expr, v = _core(e, c, weights, reusable, mapper, sregistry)

            if v:
                dump(exprs, c)
                processed.extend(v)

            if e.lhs is expr.rhs:
                # Optimization 2: `e` is of the form
                # `r = IndexDerivative(...)`
                # Rather than say
                # `r = foo(IndexDerivative(...))`
                # Since `r` is reusable (Optimization 1), we now have `r = r`,
                # which can safely be discarded
                pass
            else:
                exprs.append(expr)

        dump(exprs, c)

    return processed, weights, mapper


def _core(expr, c, weights, reusables, mapper, sregistry):
    """
    Recursively carry out the core of `lower_index_derivatives`.
    """
    if q_leaf(expr):
        return expr, []

    args = []
    processed = []
    for a in expr.args:
        e, clusters = _core(a, c, weights, reusables, mapper, sregistry)
        args.append(e)
        processed.extend(clusters)

    expr = reuse_if_untouched(expr, args)

    if not isinstance(expr, IndexDerivative):
        return expr, processed

    # Create concrete Weights and reuse them whenever possible
    name = sregistry.make_name(prefix='w')
    w0 = expr.weights.function
    k = tuple(w0.weights)
    try:
        w = weights[k]
    except KeyError:
        w = weights[k] = w0._rebuild(name=name, dtype=expr.dtype)
    expr = uxreplace(expr, {w0.indexed: w.indexed})

    dims = retrieve_dimensions(expr, deep=True)
    dims = filter_ordered(d for d in dims if isinstance(d, StencilDimension))

    dims = tuple(reversed(dims))

    # If a StencilDimension already appears in `c.ispace`, perhaps with its custom
    # upper and lower offsets, we honor it
    dims = tuple(d for d in dims if d not in c.ispace)

    intervals = [Interval(d) for d in dims]
    directions = {d: Backward if d.backward else Forward for d in dims}
    ispace0 = IterationSpace(intervals, directions=directions)

    extra = (c.ispace.itdims + dims,)
    ispace = IterationSpace.union(c.ispace, ispace0, relations=extra)

    # Set the IterationSpace along the StencilDimensions to start from 0
    # (rather than the default `d._min`) to minimize the amount of integer
    # arithmetic to calculate the various index access functions
    for d in dims:
        ispace = ispace.translate(d, -d._min)

    try:
        s = reusables.pop()
        assert s.dtype is w.dtype
    except KeyError:
        name = sregistry.make_name(prefix='r')
        s = Symbol(name=name, dtype=w.dtype)
    expr0 = Eq(s, 0.)
    ispace1 = ispace.project(lambda d: d is not dims[-1])
    processed.insert(0, c.rebuild(exprs=expr0, ispace=ispace1))

    # Transform e.g. `r0[x + i0 + 2, y] -> r0[x + i0, y, z]` for alignment
    # with the shifted `ispace`
    base = expr.base
    for d in dims:
        base = base.subs(d, d + d._min)
    expr1 = Inc(s, base*expr.weights)
    processed.append(c.rebuild(exprs=expr1, ispace=ispace))

    # Track lowered IndexDerivative for subsequent optimization by the caller
    mapper.setdefault(expr1.rhs, []).append(s)

    return s, processed


class CDE(Queue):

    """
    Common derivative elimination.
    """

    def __init__(self, mapper):
        super().__init__()

        self.mapper = {k: v for k, v in mapper.items() if len(v) > 1}

    def process(self, clusters):
        return self._process_fdta(clusters, 1, subs0={}, seen=set())

    def callback(self, clusters, prefix, subs0=None, seen=None):
        subs = {}
        processed = []
        for c in clusters:
            if c in seen:
                processed.append(c)
                continue

            exprs = []
            for e in c.exprs:
                k, v = e.args

                if k in subs0:
                    continue

                try:
                    subs0[k] = subs[v]
                    continue
                except KeyError:
                    pass

                if v in self.mapper:
                    subs[v] = k
                    exprs.append(e)
                else:
                    exprs.append(uxreplace(e, {**subs0, **subs}))

            processed.append(c.rebuild(exprs=exprs))

        seen.update(processed)

        return processed
