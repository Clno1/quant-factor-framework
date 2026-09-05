"""Render actual Jinja template with local fixtures; never open the page."""
from pathlib import Path
from html.parser import HTMLParser
import json
from jinja2 import Environment, DictLoader, select_autoescape

root=Path('/private/tmp/quant_fresh_audit_20260905/repo_trading')
out=Path('/private/tmp/quant_fresh_audit_20260905')
source=(root/'src/webapp/templates/strategy_list.html').read_text()
env=Environment(loader=DictLoader({'strategy_list.html':source,'base.html':'{% block content %}{% endblock %}'}),autoescape=select_autoescape(['html']))
class Events(HTMLParser):
    def __init__(self):super().__init__();self.onclicks=[]
    def handle_starttag(self,tag,attrs):
        for name,value in attrs:
            if name=='onclick':self.onclicks.append(value)
cases=[]
for name in ["O'Brien", "x');globalThis.auditMarker=1;//"]:
    html=env.get_template('strategy_list.html').render(items=[{'id':'12345678-1234-1234-1234-123456789abc','name':name,'description':'','n_components':1,'created_at':''}])
    parser=Events();parser.feed(html)
    cases.append({'name':name,'onclick':parser.onclicks[0]})
(out/'repro_template_names.json').write_text(json.dumps(cases,indent=2))
print(json.dumps(cases,indent=2))
