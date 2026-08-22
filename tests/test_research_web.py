from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pandas as pd

from src.factors.observations import FactorObservationError
from src.strategies import (
    StrategyComponent,
    StrategyDefinition,
    create_strategy,
)
from src.watchlists import WatchlistDefinition, WatchlistItem, create_watchlist
from src.webapp.app import create_app
from src.webapp import research_routes, routes_v2
from src.webapp.security import AUTH_PASSWORD_ENV, AUTH_USER_ENV


def test_research_information_architecture_and_target_pages(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("QUANT_APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.delenv(AUTH_USER_ENV, raising=False)
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)

    strategy = create_strategy(
        StrategyDefinition.new(
            name="Web smoke",
            description="",
            components=[StrategyComponent("MOM_12M", 1.0)],
        )
    )
    target = create_watchlist(
        WatchlistDefinition.new(
            name="Target smoke",
            items=[WatchlistItem("AAPL", 1.0)],
        )
    )
    monkeypatch.setattr(
        routes_v2,
        "generate_target_weights",
        lambda **_kwargs: SimpleNamespace(
            target_weights=pd.DataFrame([{
                "ticker": "AAPL",
                "score": 1.25,
                "group": 5,
                "target_weight": 1.0,
                "decision_price": 200.0,
            }]),
            decision_date="2026-08-07",
            effective_n_groups=5,
            top_group=5,
            normalized_weights={"MOM_12M": 1.0},
            tickers_used=["AAPL"],
            tickers_missing=[],
            warnings=[],
            data_contract={
                "requested_universe": f"watchlist:{target.id}",
                "data_universe": "WATCHLIST_TEST",
                "dataset_version_id": "dataset-ranking-v1",
                "factor_publication_id": None,
                "runtime_factor_id": "runtime:ranking-v1",
            },
        ),
    )

    class FailClosedObservationReader:
        def metadata(self, **_kwargs):
            return {
                "universes": [
                    {"universe_id": "SP500", "status": "INVALID"},
                    {"universe_id": "NASDAQ100", "status": "MISSING"},
                ],
                "available_dates": [],
            }

        def snapshot(self, *, universe, **_kwargs):
            if str(universe).upper() == "SP500":
                raise FactorObservationError(
                    "RESEARCH_INVALID",
                    "Injected invalid research publication",
                    status_code=409,
                )
            raise FactorObservationError(
                "RESEARCH_NOT_PUBLISHED",
                "Injected unpublished research universe",
                status_code=409,
            )

        def search_securities(self, **_kwargs):
            return {
                "security_master_generation_id": "security-test",
                "security_master_target_session": "2026-08-07",
                "asof": "2026-08-07",
                "query": "MDB",
                "rows": [{
                    "security_id": "sec_mdb",
                    "ticker": "MDB",
                    "name": "MongoDB, Inc.",
                    "coverage_status": "PUBLISHED",
                    "available_comparison_universes": ["US_LIQUID_5M"],
                }],
            }

    monkeypatch.setattr(
        research_routes,
        "factor_observation_reader",
        FailClosedObservationReader(),
    )
    app = create_app()

    async def exercise() -> None:
        transport = httpx.ASGITransport(
            app=app,
            client=("127.0.0.1", 12345),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://quant.test",
        ) as client:
            paths = [
                "/research",
                "/research?view=universes",
                "/research/universes/SP500",
                "/research/factors/MOM_12M",
                "/research/factor-data",
                "/strategies",
                f"/strategies/{strategy.id}",
                f"/backtests/new?strategy_id={strategy.id}&watchlist_id={target.id}",
                f"/paper/new?strategy_id={strategy.id}&watchlist_id={target.id}",
                "/decision-replay",
                "/watchlists",
                "/api/research/status",
                "/api/research/universes",
                "/api/research/universes/SP500",
                "/api/research/factors",
                "/api/research/factors/MOM_12M",
                "/api/research/factor-data/meta",
                "/api/securities/search?q=MDB&asof=2026-08-07",
                f"/api/strategies/{strategy.id}",
                f"/api/strategies/{strategy.id}/ranking?universe=watchlist:{target.id}",
                "/api/watchlists",
            ]
            responses = {path: await client.get(path) for path in paths}
            assert all(response.status_code == 200 for response in responses.values())

            retired_paths = {
                "/research/cross-universe": "/research",
                "/research/universes": "/research?view=universes",
                "/factors": "/research",
                "/rankings": "/strategies",
                f"/strategies/{strategy.id}/ranking?universe=watchlist:{target.id}": (
                    f"/strategies/{strategy.id}"
                ),
            }
            for path, destination in retired_paths.items():
                response = await client.get(path)
                assert response.status_code == 307
                assert response.headers["location"] == destination

            overview = responses["/research"].text
            assert overview.count('<details class="side-group" open>') == 4
            assert overview.count('href="/research"') >= 2
            assert 'href="/research/universes"' not in overview
            assert 'href="/factors"' not in overview
            assert "研究股票池" in overview
            assert "因子数据" in overview
            assert "目标股票池" in overview
            assert ">跨池稳健性<" not in overview
            assert ">股票排名<" not in overview
            assert ">01<" not in overview
            assert "每一行代表一个因子，也就是一种选股评分规则，不是一只股票" in overview
            assert "跨池稳健</option>" in overview
            assert "仅主研究池通过</option>" in overview
            assert "标普 500（SP500）</option>" in overview
            assert "纳斯达克 100（NASDAQ100）</option>" in overview
            assert "继续观察</option>" in overview
            assert "这是数据或研究发布状态，不是因子结论" in overview
            assert "股票池与数据" in overview
            assert "查看定义" in overview
            assert ">INSUFFICIENT</span>" not in overview
            assert ">INVALID</span>" not in overview
            assert ">MISSING</span>" not in overview

            factor_data_page = responses["/research/factor-data"].text
            assert "日期截面" in factor_data_page
            assert "单股历史" in factor_data_page
            assert "排名始终基于完整 PIT 有效截面" in factor_data_page

            factor_data_meta = responses[
                "/api/research/factor-data/meta"
            ].json()
            factor_pool_status = {
                item["universe_id"]: item["status"]
                for item in factor_data_meta["universes"]
            }
            assert factor_pool_status["SP500"] == "INVALID"
            assert factor_pool_status["NASDAQ100"] == "MISSING"
            security_search = responses[
                "/api/securities/search?q=MDB&asof=2026-08-07"
            ].json()
            assert security_search["rows"][0]["security_id"] == "sec_mdb"
            assert security_search["rows"][0][
                "available_comparison_universes"
            ] == ["US_LIQUID_5M"]

            invalid_sp500 = await client.get(
                "/api/research/factor-data/snapshot?universe=SP500&"
                "factor=MOM_12M&date=latest"
            )
            assert invalid_sp500.status_code == 409
            assert invalid_sp500.json()["detail"]["code"] == "RESEARCH_INVALID"

            unpublished_nasdaq = await client.get(
                "/api/research/factor-data/snapshot?universe=NASDAQ100&"
                "factor=MOM_12M&date=latest"
            )
            assert unpublished_nasdaq.status_code == 409
            assert (
                unpublished_nasdaq.json()["detail"]["code"]
                == "RESEARCH_NOT_PUBLISHED"
            )

            universe_page = responses["/research?view=universes"].text
            assert "全美行情覆盖回答“有没有数据”" in universe_page
            assert "全美证券行情覆盖" in universe_page
            assert "全美流动股票" in universe_page
            assert "行情覆盖层" in universe_page
            assert "宽基比较池" in universe_page
            assert "正式验证池" in universe_page
            assert "主研究池" in universe_page
            assert "按历史时点变化" in universe_page

            backtest = responses[
                f"/backtests/new?strategy_id={strategy.id}&watchlist_id={target.id}"
            ].text
            assert "strategyResearchEvidence" in backtest
            assert "NASDAQ100" in backtest

            ranking = responses[
                f"/api/strategies/{strategy.id}/ranking?universe=watchlist:{target.id}"
            ].json()
            assert ranking["data_contract"]["data_universe"] == "WATCHLIST_TEST"
            assert ranking["data_contract"]["dataset_version_id"] == "dataset-ranking-v1"
            assert (
                ranking["target_universe_snapshot"]["ticker_revision_sha256"]
                == target.ticker_revision_sha256()
            )

            target_page = responses["/watchlists"].text
            assert "股票清单版本" in target_page
            assert "待补齐" in target_page
            assert "尚无专属行情版本" in target_page
            assert "ticker revision" not in target_page
            assert ">TARGET<" not in target_page
            assert ">MISSING<" not in target_page
            assert "最近排名" not in target_page

            strategy_page = responses[f"/strategies/{strategy.id}"].text
            assert "查看股票排行" not in strategy_page
            assert ">INSUFFICIENT</span>" not in strategy_page

            watchlists = responses["/api/watchlists"].json()
            assert watchlists[0]["universe_type"] == "TARGET"
            assert watchlists[0]["ticker_revision_sha256"].startswith("sha256:")

    asyncio.run(exercise())
