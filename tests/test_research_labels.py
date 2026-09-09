from src.webapp.research_labels import (
    factor_direction_label,
    factor_input_label,
    research_label,
    research_text,
    target_data_status_label,
    target_data_status_note,
    universe_label,
)


def test_research_codes_have_clear_chinese_presentation_labels():
    assert research_label("ROBUST") == "跨池稳健"
    assert research_label("PRIMARY_ONLY") == "仅主研究池通过"
    assert research_label("PASS") == "通过"
    assert research_label("INVALID") == "无效"
    assert research_label("PRIMARY") == "主研究池"
    assert research_label("PIT") == "按历史时点变化"


def test_research_labels_keep_stable_ids_visible_where_they_add_context():
    assert universe_label("SP500") == "标普 500（SP500）"
    assert universe_label("NASDAQ100") == "纳斯达克 100（NASDAQ100）"
    assert universe_label("CUSTOM_POOL") == "CUSTOM_POOL"
    assert factor_input_label("adj_close") == "复权收盘价"
    assert factor_direction_label(1).startswith("正向")


def test_target_market_data_statuses_are_actionable_chinese_labels():
    assert target_data_status_label("PUBLISHED") == "已就绪"
    assert target_data_status_label("MISSING") == "待补齐"
    assert target_data_status_label("STALE") == "需更新"
    assert target_data_status_label("INVALID") == "校验失败"
    assert "补数队列" in target_data_status_note("MISSING")


def test_existing_publication_messages_are_localized_without_rewriting_data():
    message = "必要研究证据不完整：SP500:INVALID, NASDAQ100:MISSING"
    assert research_text(message) == (
        "必要研究证据不完整：标普500：无效, 纳斯达克100：缺失"
    )
