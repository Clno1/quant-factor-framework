"""FastAPI Web service package.

注意：这里故意不在包级别 import app / create_app。
原因：create_app() 在执行时会反向 import 回 src.backtest.runner，
而 src.backtest 的子模块（composer）又依赖 src.webapp.results_store，
若 __init__ 触发 app 构造就会形成循环导入。

需要 app 对象时，请显式： from src.webapp.app import app, create_app
"""
