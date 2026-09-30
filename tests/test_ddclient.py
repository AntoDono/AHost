from ahost import ddclient

CONF = """daemon=300\t\t\t\t# check every 300 seconds
ssl=yes
use=web, web=ifconfig.me/ip
protocol=namecheap
server=dynamicdns.park-your-domain.com
login=example.com
password=SECRET1
@.example.com
www.example.com
app.example.com, old.example.com
protocol=namecheap
server=dynamicdns.park-your-domain.com
login=other.net
password='SECRET2'
api.other.net
"""


def test_sync_rewrites_only_host_lines():
    plan = ddclient.sync(CONF, {"app.example.com", "new.example.com", "vpn.example.com", "api.other.net",
                                "x.unknown.org"})
    assert plan.added == {"example.com": ["new.example.com", "vpn.example.com"], "other.net": []}
    assert plan.removed == {"example.com": ["@.example.com", "old.example.com", "www.example.com"], "other.net": []}
    assert plan.unplaceable == ["x.unknown.org"]
    # credentials and settings untouched, in place
    for keep in ("password=SECRET1", "password='SECRET2'", "login=example.com", "daemon=300\t\t\t\t# check every 300 seconds"):
        assert keep in plan.new_text
    lines = plan.new_text.splitlines()
    assert lines.index("app.example.com") > lines.index("password=SECRET1")
    assert lines.index("api.other.net") > lines.index("password='SECRET2'")


def test_sync_noop():
    plan = ddclient.sync(CONF, {"@.example.com", "www.example.com", "app.example.com", "old.example.com",
                                "api.other.net"})
    assert not plan.changed
