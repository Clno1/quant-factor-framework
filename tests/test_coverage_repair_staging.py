import json

import duckdb
import pandas as pd
import pytest

from scripts.update_us_equity_coverage import _sha256, _stage_replacement_history
from src.data.foundation import DataFoundationError


def sources(root, count=27):
    paths, proofs, frames = [], [], []
    for i in range(count):
        folder = root / str(i)
        folder.mkdir()
        frame = pd.DataFrame({'date': pd.to_datetime(['2020-01-31', '2020-02-03']),
                              'security_id': [str(i)] * 2, 'close': [1.125 + i, 2.375 + i]})
        path = folder / 'bars.parquet'
        frame.to_parquet(path, index=False)
        manifest = folder / 'manifest.json'
        manifest.write_text(json.dumps({'artifact': path.name, 'sha256': _sha256(path)}))
        paths.append(path)
        proofs.append({'manifest_path': str(manifest), 'manifest_sha256': _sha256(manifest), 'rows': 2})
        frames.append(frame)
    return paths, proofs, pd.concat(frames, ignore_index=True)


def test_bounded_staging_preserves_exact_rows_across_months(tmp_path, monkeypatch):
    paths, proofs, expected = sources(tmp_path)
    original = duckdb.connect
    batch_sizes = []

    class Connection:
        def __init__(self, *a, **kw):
            self.connection = original(*a, **kw)

        def execute(self, sql, parameters=None):
            if 'read_parquet' in sql:
                batch_sizes.append(len(parameters[0]))
            return self.connection.execute(sql, parameters) if parameters is not None else self.connection.execute(sql)

        def close(self):
            self.connection.close()

    monkeypatch.setattr(duckdb, 'connect', Connection)
    staged = _stage_replacement_history(paths, proofs, tmp_path)
    assert batch_sizes == [25, 2]
    assert staged['rows'] == 54 and staged['files'] == 27
    with original(staged['path'], read_only=True) as connection:
        actual = connection.execute('SELECT * FROM replacements').fetchdf()
        assert connection.execute("SELECT count(*) FROM replacements WHERE date BETWEEN '2020-02-01' AND '2020-02-29'").fetchone()[0] == 27
    pd.testing.assert_frame_equal(actual.sort_values(['security_id', 'date']).reset_index(drop=True),
                                  expected.sort_values(['security_id', 'date']).reset_index(drop=True), check_dtype=False)
    assert _sha256(tmp_path / 'replacement_history.duckdb') == staged['sha256']


@pytest.mark.parametrize('change', ['manifest', 'source', 'binding', 'count', 'scope', 'batch'])
def test_staging_rejects_changed_or_incomplete_sources(tmp_path, change):
    paths, proofs, _ = sources(tmp_path, count=1)
    kwargs = {}
    if change == 'manifest':
        proofs[0]['manifest_sha256'] = '0' * 64
    elif change == 'source':
        pd.DataFrame({'bad': [1]}).to_parquet(paths[0])
    elif change == 'binding':
        paths[0] = tmp_path / 'foreign.parquet'
    elif change == 'count':
        proofs[0]['rows'] = 3
    elif change == 'scope':
        proofs = []
    elif change == 'batch':
        kwargs['batch_size'] = 26
    with pytest.raises(DataFoundationError):
        _stage_replacement_history(paths, proofs, tmp_path, **kwargs)


def test_empty_repairs_need_no_staging(tmp_path):
    assert _stage_replacement_history([], [], tmp_path) is None
    assert not (tmp_path / 'replacement_history.duckdb').exists()
