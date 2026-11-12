"""In-memory stand-in for the supabase client, enough for the advisor's
query shapes: select/eq/neq/gt/gte/lt/lte/in_/is_ (incl. not_.is_), order,
limit, range, upsert (on_conflict, ignore_duplicates), insert, update,
delete. Rows are plain dicts; tables are created on first use."""


class _Res:
    def __init__(self, data):
        self.data = data
        self.count = len(data)


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self._order, self._limit = db, table, [], [], None
        self._range, self._negate, self.op, self.payload, self.kw = None, False, 'select', None, {}

    # filters
    def _f(self, fn):
        neg, self._negate = self._negate, False
        self.filters.append((lambda r: not fn(r)) if neg else fn)
        return self

    def eq(self, c, v): return self._f(lambda r: r.get(c) == v)
    def neq(self, c, v): return self._f(lambda r: r.get(c) != v)
    def gt(self, c, v): return self._f(lambda r: r.get(c) is not None and str(r[c]) > str(v))
    def gte(self, c, v): return self._f(lambda r: r.get(c) is not None and str(r[c]) >= str(v))
    def lt(self, c, v): return self._f(lambda r: r.get(c) is not None and str(r[c]) < str(v))
    def lte(self, c, v): return self._f(lambda r: r.get(c) is not None and str(r[c]) <= str(v))
    def in_(self, c, vs): return self._f(lambda r: r.get(c) in vs)

    def is_(self, c, v):
        return self._f(lambda r: r.get(c) is None) if v == 'null' else self._f(lambda r: r.get(c) == v)

    @property
    def not_(self):
        self._negate = True
        return self

    # shaping
    def select(self, *a, **kw): return self
    def order(self, c, desc=False): self._order.append((c, desc)); return self
    def limit(self, n): self._limit = n; return self
    def range(self, a, b): self._range = (a, b); return self
    def single(self): self._limit = 1; return self

    # writes
    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload = 'upsert', rows if isinstance(rows, list) else [rows]
        self.kw = {'on_conflict': on_conflict, 'ignore_duplicates': ignore_duplicates}
        return self

    def insert(self, rows):
        self.op, self.payload = 'insert', rows if isinstance(rows, list) else [rows]
        return self

    def update(self, vals): self.op, self.payload = 'update', vals; return self
    def delete(self): self.op = 'delete'; return self

    def _match(self, r): return all(f(r) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.calls.append((self.table, self.op, self.kw))
        if self.op == 'upsert':
            default = 'key' if self.table == 'app_config' else 'id'
            keys = [k.strip() for k in (self.kw['on_conflict'] or default).split(',')]
            for new in self.payload:
                hit = None if any(new.get(k) is None for k in keys) else \
                    next((r for r in rows if all(r.get(k) == new.get(k) for k in keys)), None)
                if hit is None:
                    rows.append(dict(new))
                elif not self.kw['ignore_duplicates']:
                    hit.update(new)
            return _Res(self.payload)
        if self.op == 'insert':
            rows.extend(dict(r) for r in self.payload)
            return _Res(self.payload)
        if self.op == 'update':
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self.payload)
            return _Res(hit)
        if self.op == 'delete':
            hit = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return _Res(hit)
        out = [dict(r) for r in rows if self._match(r)]
        for c, desc in reversed(self._order):
            out.sort(key=lambda r: (r.get(c) is None, str(r.get(c))), reverse=desc)
        if self._range:
            out = out[self._range[0]:self._range[1] + 1]
        if self._limit is not None:
            out = out[:self._limit]
        return _Res(out)


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.calls = []

    def table(self, name):
        return _Query(self, name)
