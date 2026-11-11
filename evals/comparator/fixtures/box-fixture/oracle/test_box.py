from box import Box


def test_put_get():
    b = Box()
    b.put("a", 1)
    assert b.get("a") == 1


def test_count():
    b = Box()
    b.put("a", 1)
    b.put("b", 2)
    assert b.count() == 2


def test_clear():
    b = Box()
    b.put("a", 1)
    b.clear()
    assert b.count() == 0
