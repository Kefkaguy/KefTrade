import pytest

from app.cli.opening_repricing_audit import ARCHITECTURE, _symbols, parser


def test_vps_audit_defaults_to_the_full_predeclared_30m_family_grid():
    args = parser().parse_args([])

    assert ARCHITECTURE == "opening_repricing_flow_v1"
    assert args.max_variants == 8
    assert args.max_symbols == 10
    assert args.dataset_id is None
    assert args.no_persist is False


def test_vps_audit_normalizes_an_explicit_symbol_list():
    assert _symbols(" nvda, SPY ,,qqq ") == ["NVDA", "SPY", "QQQ"]


def test_vps_audit_rejects_a_variant_count_beyond_the_declared_grid():
    with pytest.raises(SystemExit):
        parser().parse_args(["--max-variants", "9"])
