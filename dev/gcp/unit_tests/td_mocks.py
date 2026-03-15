"""
Shared mock objects for TD and Firestore types.
Import this from test modules — conftest.py re-exports fixtures that use these.
"""

import datetime
from unittest.mock import MagicMock


# ═══════════════════════════════════════════════════════════════════════════
# Firestore type simulators
# ═══════════════════════════════════════════════════════════════════════════

class FakeDatetimeWithNanoseconds(datetime.datetime):
    """Simulates google.api_core.datetime_helpers.DatetimeWithNanoseconds."""
    _nanosecond = 0

    def __new__(cls, *args, nanosecond=0, **kwargs):
        inst = super().__new__(cls, *args, **kwargs)
        inst._nanosecond = nanosecond
        return inst

    @property
    def nanosecond(self):
        return self._nanosecond

    def isoformat(self, *args, **kwargs):
        base = super().isoformat(*args, **kwargs)
        if self._nanosecond:
            return f"{base}.{self._nanosecond:09d}Z"
        return base


class FakeDocumentReference:
    """Simulates google.cloud.firestore_v1.document.DocumentReference."""

    def __init__(self, path, doc_id=None):
        self.path = path
        self.id = doc_id or path.split('/')[-1]

    def __repr__(self):
        return f'FakeDocumentReference({self.path!r})'


class FakeGeoPoint:
    """Simulates google.cloud.firestore_v1.types.GeoPoint."""

    def __init__(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude

    def __repr__(self):
        return f'GeoPoint({self.latitude}, {self.longitude})'

    def __str__(self):
        return f'GeoPoint({self.latitude}, {self.longitude})'


class FakeDocumentChange:
    """Simulates a Firestore DocumentChange from on_snapshot."""

    class _ChangeType:
        def __init__(self, name):
            self.name = name

    def __init__(self, doc_id, data, change_type='ADDED', update_time=None):
        self.document = FakeDocumentSnapshot(doc_id, data, update_time)
        self.type = self._ChangeType(change_type)


class FakeDocumentSnapshot:
    """Simulates a Firestore DocumentSnapshot."""

    def __init__(self, doc_id, data, update_time=None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self.update_time = update_time

    def to_dict(self):
        return self._data


class FakeWriteResult:
    """Simulates a Firestore WriteResult."""

    def __init__(self, update_time=None):
        self.update_time = update_time


# ═══════════════════════════════════════════════════════════════════════════
# TouchDesigner mock objects
# ═══════════════════════════════════════════════════════════════════════════

class MockCell:
    """Simulates a TD table cell.

    Supports subscript access (cell[0] returns self) to handle code
    that treats findCell result as both a cell and a list.
    """

    def __init__(self, value='', row=0, col=0):
        self.val = str(value)
        self.row = row
        self.col = col

    def __getitem__(self, key):
        if key == 0:
            return self
        raise IndexError(key)

    def __bool__(self):
        return True


class MockTableDAT:
    """
    Simulates a TouchDesigner tableDAT with rows, columns, findCell, etc.
    """

    def __init__(self, name='table1'):
        self.name = name
        self._rows = []

    @property
    def numRows(self):
        return len(self._rows)

    @property
    def numCols(self):
        return len(self._rows[0]) if self._rows else 0

    def appendRow(self, values):
        self._rows.append([str(v) for v in values])

    def deleteRow(self, index):
        if 0 <= index < len(self._rows):
            self._rows.pop(index)

    def clear(self, keepFirstRow=False):
        if keepFirstRow and self._rows:
            self._rows = [self._rows[0]]
        else:
            self._rows = []

    def row(self, index):
        if 0 <= index < len(self._rows):
            return [MockCell(v, row=index, col=c) for c, v in enumerate(self._rows[index])]
        return []

    def findCell(self, value, cols=None):
        """Return first matching cell or None (matches TD behavior)."""
        for r, row_data in enumerate(self._rows):
            search_cols = cols if cols is not None else range(len(row_data))
            for c in search_cols:
                if c < len(row_data) and row_data[c] == str(value):
                    return MockCell(row_data[c], row=r, col=c)
        return None

    def __getitem__(self, key):
        if isinstance(key, tuple):
            row, col = key
            if isinstance(col, str):
                if self._rows:
                    headers = self._rows[0]
                    try:
                        col_idx = headers.index(col)
                    except ValueError:
                        return MockCell('')
                    if 0 <= row < len(self._rows):
                        return MockCell(self._rows[row][col_idx], row=row, col=col_idx)
                return MockCell('')
            if 0 <= row < len(self._rows) and 0 <= col < len(self._rows[row]):
                return MockCell(self._rows[row][col], row=row, col=col)
            return MockCell('')
        return MockCell('')

    def __setitem__(self, key, value):
        if isinstance(key, tuple):
            row, col = key
            if isinstance(col, str):
                if self._rows:
                    headers = self._rows[0]
                    try:
                        col_idx = headers.index(col)
                    except ValueError:
                        return
                    if 0 <= row < len(self._rows):
                        self._rows[row][col_idx] = str(value)
                return
            if 0 <= row < len(self._rows) and 0 <= col < len(self._rows[row]):
                self._rows[row][col] = str(value)


class MockParameter:
    """Simulates a TD parameter that returns a fixed value on .eval()."""

    def __init__(self, value):
        self._value = value

    def eval(self):
        return self._value


class MockOwnerComp:
    """
    Simulates a TD COMP that owns extensions.
    Provides par.*, op(), ext.*, and name.
    """

    def __init__(self, name='firestore', pars=None):
        self.name = name
        self._ops = {}
        self._pars = pars or {}
        self.ext = MagicMock()
        self.par = MagicMock()

        for k, v in self._pars.items():
            setattr(self.par, k, MockParameter(v))

    def op(self, path):
        return self._ops.get(path)

    def register_op(self, path, dat):
        self._ops[path] = dat

    def create(self, op_type, name):
        dat = MockTableDAT(name)
        self._ops[name] = dat
        return dat