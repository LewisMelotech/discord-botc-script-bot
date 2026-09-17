import pytest

from botcbot.minecraft import commands_for


def test_both_commands_are_built_from_the_custom_id():
    assert commands_for("sects") == (
        "/function botc_nw_lite:roles/sects",
        "/function botc_nw_lite:scripts/sects",
    )


def test_roles_comes_before_scripts():
    roles, scripts = commands_for("tb")
    assert ":roles/" in roles
    assert ":scripts/" in scripts


def test_a_hyphenated_custom_id_is_used_as_it_is():
    assert commands_for("sects-and-violets")[1] == (
        "/function botc_nw_lite:scripts/sects-and-violets"
    )


@pytest.mark.parametrize("custom_id", [None, ""])
def test_no_custom_id_means_no_commands(custom_id):
    assert commands_for(custom_id) == ()
