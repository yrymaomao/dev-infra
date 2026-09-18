"""Public CSV row feedback must identify the actual rejected constraint."""

from ebiz_deployment.supply_chain_bff.selection_csv import parse_selection_csv


def test_overlong_sku_explains_length_without_losing_following_valid_row():
    source = "sku,fulfillment_mode,fba_ratio,fbm_ratio\n" + "A" * 129 + ",AUTO,,\nVALID_1,AUTO,,"
    result = parse_selection_csv(source.encode())
    assert [row.sku for row in result.rows] == ["VALID_1"]
    assert len(result.errors) == 1
    assert result.errors[0].row == 2
    assert "128" in result.errors[0].message
    assert "length" in result.errors[0].message.lower()


def test_maximum_length_sku_is_still_accepted():
    source = "sku,fulfillment_mode,fba_ratio,fbm_ratio\n" + "A" * 128 + ",AUTO,,"
    result = parse_selection_csv(source.encode())
    assert len(result.rows) == 1
    assert not result.errors
