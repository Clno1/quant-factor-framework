from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

from jinja2 import Environment, select_autoescape
import pandas as pd
import pytest


def test_nonempty_outcome_audit_records_are_json_serializable():
    from src.webapp.routes import _json_records
    assert _json_records(pd.DataFrame([{"ticker": "AAPL", "return": float("nan")}])) == [{"ticker": "AAPL", "return": None}]


@pytest.mark.parametrize("template, key", [
    ("strategy_list", "it"), ("strategy_detail", "strategy"),
    ("backtest_list", "t"), ("paper_list", "a"), ("paper_detail", "account"),
])
@pytest.mark.parametrize("name", ["O'Brien", 'x\');globalThis.auditMarker=1;//"<>&\n'])
def test_delete_buttons_treat_names_as_data(template, key, name):
    # Render the actual button markup and decode HTML attributes exactly as a
    # browser does before compiling the event handler.
    source = (Path(__file__).resolve().parents[1] / "src/webapp/templates" / f"{template}.html").read_text()
    button = re.search(r'<button[^>]*onclick="delete(?:Strategy|Backtest|Paper)\([^>]*>.*?</button>', source).group()
    rendered = Environment(autoescape=select_autoescape(default=True)).from_string(button).render(
        **{key: {"id": "12345678-1234-1234-1234-123456789abc", "name": name}})
    class Parser(HTMLParser):
        attrs = {}
        def handle_starttag(self, tag, attrs):
            if tag == "button":
                self.attrs = dict(attrs)
    parser = Parser()
    parser.feed(rendered)
    assert parser.attrs["data-name"] == name
    assert name not in parser.attrs["onclick"]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for native event-handler execution")
    script = """
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let called;
const capture = (id, name) => { called = {id, name}; };
const sandbox = {deleteStrategy: capture, deleteBacktest: capture, deletePaper: capture,
                 button: {dataset: {id: input['data-id'], name: input['data-name']}}};
vm.runInNewContext('(function(){' + input.onclick + '}).call(button)', sandbox, {timeout: 1000});
process.stdout.write(JSON.stringify({called, injected: sandbox.auditMarker === 1}));
"""
    result = subprocess.run([node, "-e", script], input=json.dumps(parser.attrs), text=True, capture_output=True, check=True)
    decoded = json.loads(result.stdout)
    assert decoded == {"called": {"id": parser.attrs["data-id"], "name": name}, "injected": False}
