from cob import at


def test_parse_line():
    assert at.parse_line("+CIEV: 2,1") == ("+CIEV", "2,1")
    assert at.parse_line("RING") == ("RING", "")
    assert at.parse_line("+CME ERROR: 30") == ("+CME ERROR", "30")


def test_split_args_respects_quotes_and_parentheses():
    assert at.split_args('"a,b",1,(0,1)') == ['"a,b"', "1", "(0,1)"]
    assert at.split_args("") == []


def test_parse_cind_test():
    args = '("service",(0,1)),("call",(0,1)),("call_setup",(0-3)),("battchg",(0-5))'
    assert at.parse_cind_test(args) == ["service", "call", "callsetup", "battchg"]


def test_parse_clip_and_cops():
    assert at.parse_clip('"+905551112233",145,,,"Mom"') == "+905551112233"
    assert at.parse_clip('"",128') is None
    assert at.parse_cops('0,0,"Operator Name"') == "Operator Name"
    assert at.parse_cops("0") is None


def test_parse_clcc():
    call = at.parse_clcc('1,0,0,0,0,"+15551234",145')
    assert call == at.Call(1, True, "active", False, "+15551234")
    assert at.parse_clcc("2,1,4,0,1").status == "incoming"
    assert at.parse_clcc("bad") is None


def test_parse_chld_and_dialable():
    assert at.parse_chld_test("(0,1,1x,2,2x,3,4)") == ["0", "1", "1x", "2", "2x", "3", "4"]
    assert at.is_dialable("+90*31#")
    assert not at.is_dialable("12 34")
