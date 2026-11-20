"""
Tests for astroquery.srcnet.user_storage: Cavern (VOSpace) discovery from Site
Capabilities, path handling, node parsing, listing and downloads, with mocked HTTP.
"""
import base64
import json
import os
import types
from unittest.mock import MagicMock, patch

import pytest

from astroquery.srcnet.user_storage import (
    UserStorageClass,
    _cavern_url,
    _is_vospace,
    _node_path,
    _SiteCapabilities,
    _storage_services,
)

BASE = "https://canfar.example.org/cavern"

LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<vos:node xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  uri="vos://example.org~cavern/home/alice" xsi:type="vos:ContainerNode">
  <vos:properties>
    <vos:property uri="ivo://ivoa.net/vospace/core#date">2026-11-01T10:00:00.000</vos:property>
  </vos:properties>
  <vos:nodes>
    <vos:node uri="vos://example.org~cavern/home/alice/results" xsi:type="vos:ContainerNode">
      <vos:properties>
        <vos:property uri="ivo://ivoa.net/vospace/core#date">2026-11-02T10:00:00.000</vos:property>
        <vos:property uri="ivo://ivoa.net/vospace/core#ispublic">true</vos:property>
      </vos:properties>
    </vos:node>
    <vos:node uri="vos://example.org~cavern/home/alice/cube.fits" xsi:type="vos:DataNode">
      <vos:properties>
        <vos:property uri="ivo://ivoa.net/vospace/core#length">2880</vos:property>
        <vos:property uri="ivo://ivoa.net/vospace/core#date">2026-11-03T10:00:00.000</vos:property>
        <vos:property uri="ivo://ivoa.net/vospace/core#type">application/fits</vos:property>
      </vos:properties>
    </vos:node>
  </vos:nodes>
</vos:node>"""

EMPTY = LISTING.split("<vos:nodes>")[0] + "<vos:nodes/></vos:node>"


def _token(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _resp(status=200, text="", content=None, chunks=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text
    r.content = content if content is not None else text.encode()
    r.iter_content.return_value = chunks or []
    r.raise_for_status.side_effect = None if r.ok else Exception(f"HTTP {status}")
    return r


@pytest.fixture
def srcnet():
    client = MagicMock()
    client.access_token = _token(preferred_username="alice", aud="authn-api", exp=4102444800)
    client.refresh_token = "r"
    client.srcnet_site_capabilities_url = "https://sc.example/api/v1"
    return client


@pytest.fixture
def us(srcnet):
    store = UserStorageClass(srcnet, BASE + "/")
    # Token refresh/exchange are exercised in core's own tests.
    store._headers = lambda: {"Authorization": "Bearer t"}
    return store


class TestNodePath:

    @pytest.mark.parametrize("given, expected", [
        ("home/alice", "home/alice"),
        ("/home/alice/", "home/alice"),
        ("vos://example.org~cavern/home/alice/x.fits", "home/alice/x.fits"),
        ("", ""),
    ])
    def test_normalises(self, given, expected):
        assert _node_path(given) == expected

    @pytest.mark.parametrize("bad", ["home/../etc", "home//alice", "home/./alice"])
    def test_refuses_dot_and_empty_segments(self, bad):
        with pytest.raises(ValueError):
            _node_path(bad)

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            _node_path(None)


class TestCavernUrl:

    @pytest.mark.parametrize("service, expected", [
        ({"prefix": "https", "host": "src.canfar.net", "path": "cavern"}, "https://src.canfar.net/cavern"),
        ({"prefix": "https://", "host": "h.org", "path": "/cavern/"}, "https://h.org/cavern"),
        ({"prefix": "https", "host": "h.org", "port": 443, "path": "/cavern"}, "https://h.org/cavern"),
        ({"prefix": "https", "host": "h.org", "port": 8443, "path": "/cavern"}, "https://h.org:8443/cavern"),
        # path packed into host, port given separately (seen in production)
        ({"prefix": "https", "host": "h.org/storage", "port": 443}, "https://h.org/storage"),
    ])
    def test_shapes_seen_in_site_capabilities(self, service, expected):
        assert _cavern_url(service) == expected


def _nodes():
    def site(name, services, compute_disabled=False):
        compute = {"is_force_disabled": compute_disabled, "associated_local_services": services}
        return {"name": name, "compute": [compute]}
    return [
        {"name": "AAA", "sites": [site("AAA-1", [
            {"type": "cavern", "prefix": "https", "host": "a.org", "path": "cavern"},
            {"type": "canfar", "prefix": "https", "host": "a.org", "path": "/science-portal"},
        ])]},
        {"name": "BBB", "sites": [site("BBB-1", [
            {"type": "canfar", "prefix": "https", "host": "b.org/science-portal/", "port": 443},
        ])]},
        {"name": "CCC", "sites": [site("CCC-1", [
            {"type": "canfar", "prefix": "https", "host": "c.org", "is_force_disabled": True},
        ])]},
        {"name": "DDD", "sites": [site("DDD-1", [
            {"type": "canfar", "prefix": "https", "host": "d.org"},
        ], compute_disabled=True)]},
        {"name": "EEE", "sites": [{"name": "EEE-1", "storages": []}]},
    ]


class TestStorageServices:

    def test_registered_then_portal_fallback(self):
        rows = _storage_services(_nodes())
        assert [(r["node"], r["url"], r["registered"]) for r in rows] == [
            ("AAA", "https://a.org/cavern", True),
            ("BBB", "https://b.org/cavern", False),
        ]

    def test_disabled_services_and_compute_skipped(self):
        nodes = {r["node"] for r in _storage_services(_nodes())}
        assert "CCC" not in nodes and "DDD" not in nodes and "EEE" not in nodes


class TestFind:

    def _sc(self, srcnet, nodes, vospace_urls):
        sc = _SiteCapabilities(srcnet)
        sc.nodes = lambda: nodes
        return sc, patch("astroquery.srcnet.user_storage._is_vospace",
                         side_effect=lambda session, url: url in vospace_urls)

    def test_picks_first_address_that_answers(self, srcnet):
        nodes = [{"name": "X", "sites": [{"name": "X-1", "compute": [{"associated_local_services": [
            {"type": "cavern", "prefix": "https", "host": "x.org/storage"},     # registered but wrong
            {"type": "canfar", "prefix": "https", "host": "x.org", "path": "/science-portal"},
        ]}]}]}]
        sc, p = self._sc(srcnet, nodes, {"https://x.org/cavern"})
        with p:
            assert sc.find("x") == "https://x.org/cavern"

    def test_site_name_also_matches(self, srcnet):
        sc, p = self._sc(srcnet, _nodes(), {"https://a.org/cavern"})
        with p:
            assert sc.find("aaa-1") == "https://a.org/cavern"

    def test_unknown_site(self, srcnet):
        sc, p = self._sc(srcnet, _nodes(), set())
        with p, pytest.raises(ValueError, match="No user storage found"):
            sc.find("ZZZ")

    def test_none_answering(self, srcnet):
        sc, p = self._sc(srcnet, _nodes(), set())
        with p, pytest.raises(ValueError, match="answers as a VOSpace"):
            sc.find("AAA")


class TestIsVospace:

    def test_true_for_vospace_capabilities(self):
        session = MagicMock()
        session.get.return_value = _resp(200, '<cap standardID="ivo://ivoa.net/std/VOSpace/v2.0#nodes"/>')
        assert _is_vospace(session, BASE)
        # no bearer token sent while probing
        assert session.get.call_args.kwargs["headers"] == {"Authorization": None}

    def test_false_for_other_pages_and_errors(self):
        session = MagicMock()
        session.get.return_value = _resp(200, "<html>storage</html>")
        assert not _is_vospace(session, BASE)
        session.get.side_effect = ConnectionError
        assert not _is_vospace(session, BASE)


class TestUserStorage:

    def test_home_from_token(self, us):
        assert us.username == "alice"
        assert us.home == "home/alice"

    def test_home_without_username(self, srcnet):
        srcnet.access_token = None
        with pytest.raises(ValueError, match="login"):
            UserStorageClass(srcnet, BASE).home

    def test_list_defaults_to_home_and_parses_children(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, LISTING)
        table = us.list()
        url = srcnet.session.request.call_args.args[1]
        assert url == BASE + "/nodes/home/alice"
        assert srcnet.session.request.call_args.kwargs["params"] == {"detail": "max"}
        assert list(table["name"]) == ["results", "cube.fits"]
        assert list(table["type"]) == ["folder", "file"]
        assert list(table["size"]) == [-1, 2880]
        assert list(table["content_type"]) == ["", "application/fits"]
        assert list(table["is_public"]) == [True, False]
        assert table["path"][1] == "home/alice/cube.fits"

    def test_list_empty_folder(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, EMPTY)
        table = us.list("home/alice/empty")
        assert len(table) == 0
        assert "name" in table.colnames

    def test_path_segments_are_quoted(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, EMPTY)
        us.list("home/alice/my results#1")
        assert srcnet.session.request.call_args.args[1] == BASE + "/nodes/home/alice/my%20results%231"

    def test_info(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, LISTING)
        info = us.info("home/alice")
        assert info["type"] == "folder" and info["path"] == "home/alice"

    def test_not_found(self, us, srcnet):
        srcnet.session.request.return_value = _resp(404, "NotFound")
        with pytest.raises(FileNotFoundError):
            us.info("home/alice/nope")
        assert us.exists("home/alice/nope") is False

    def test_missing_home_explained(self, us, srcnet):
        srcnet.session.request.return_value = _resp(404, "NodeNotFound")
        with pytest.raises(FileNotFoundError, match="no home folder"):
            us.list()
        with pytest.raises(FileNotFoundError, match="Not found"):
            us.list("home/alice/sub")

    def test_permission_error(self, us, srcnet):
        srcnet.session.request.return_value = _resp(403, "PermissionDenied")
        with pytest.raises(PermissionError, match="403"):
            us.list("home/bob")

    def test_not_vospace_xml(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, "<html")
        with pytest.raises(ValueError, match="VOSpace"):
            us.list()

    def test_get_writes_file_atomically(self, us, srcnet, tmp_path):
        srcnet.session.request.return_value = _resp(200, chunks=[b"SIMPLE", b"  = T"])
        out = tmp_path / "sub" / "cube.fits"
        assert us.get("home/alice/cube.fits", str(out)) == str(out)
        assert out.read_bytes() == b"SIMPLE  = T"
        assert srcnet.session.request.call_args.args[1] == BASE + "/files/home/alice/cube.fits"
        assert srcnet.session.request.call_args.kwargs["stream"] is True
        assert not [p for p in os.listdir(out.parent) if p.endswith(".part")]

    def test_get_default_name(self, us, srcnet, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        srcnet.session.request.return_value = _resp(200, chunks=[b"x"])
        assert us.get("home/alice/cube.fits") == "cube.fits"
        assert (tmp_path / "cube.fits").read_bytes() == b"x"

    def test_get_failure_leaves_no_partial(self, us, srcnet, tmp_path):
        r = _resp(200)
        r.iter_content.side_effect = IOError("connection reset")
        srcnet.session.request.return_value = r
        with pytest.raises(IOError):
            us.get("home/alice/cube.fits", str(tmp_path / "cube.fits"))
        assert os.listdir(tmp_path) == []

    def test_headers_sent(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, EMPTY)
        us.list("home/alice")
        assert srcnet.session.request.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}


class TestFactory:

    def test_needs_site_or_url(self):
        from astroquery.srcnet import SRCNetClass
        client = SRCNetClass(access_token="x", refresh_token="y")
        with pytest.raises(ValueError):
            client.get_user_storage()
        with pytest.raises(ValueError):
            client.get_user_storage("ESPSRC", url=BASE)

    def test_url_skips_lookup(self):
        from astroquery.srcnet import SRCNetClass
        client = SRCNetClass(access_token="x", refresh_token="y")
        store = client.get_user_storage(url=BASE)
        assert isinstance(store, UserStorageClass) and store.url == BASE


ROOT = """<vos:node xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" uri="vos://example.org~cavern" xsi:type="vos:ContainerNode"/>"""


def _node(path, node_type="vos:DataNode", public="false"):
    return (f'<vos:node xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" uri="vos://example.org~cavern/{path}" '
            f'xsi:type="{node_type}"><vos:properties><vos:property uri="ivo://ivoa.net/vospace/core#ispublic">'
            f'{public}</vos:property></vos:properties></vos:node>')


def _job(phase, message=None):
    err = f"<uws:errorSummary><uws:message>{message}</uws:message></uws:errorSummary>" if message else ""
    return f'<uws:job xmlns:uws="http://www.ivoa.net/xml/UWS/v1.0"><uws:phase>{phase}</uws:phase>{err}</uws:job>'


class FakeCavern:
    """Routes session.request(method, url, ...) by (method, path below BASE); records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kwargs):
        key = (method, url.replace(BASE, "").split("?")[0])
        self.calls.append((method, url, kwargs))
        answer = self.routes.get(key, _resp(404, "NodeNotFound"))
        # MagicMock responses are callable too, so only call real functions
        return answer(kwargs) if isinstance(answer, types.FunctionType) else answer


@pytest.fixture
def rw(srcnet):
    store = UserStorageClass(srcnet, BASE)
    store._headers = lambda: {"Authorization": "Bearer t"}
    return store


def _redirect(location):
    r = _resp(303)
    r.headers = {"Location": location}
    return r


class TestWrites:

    def test_mkdir_sends_container_node(self, rw, srcnet):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT), ("PUT", "/nodes/home/alice/new"): _resp(201)})
        srcnet.session.request.side_effect = fake
        assert rw.mkdir("home/alice/new") == "home/alice/new"
        method, url, kw = fake.calls[-1]
        body = kw["data"].decode()
        assert method == "PUT" and kw["headers"]["Content-Type"] == "text/xml"
        assert 'xsi:type="vos:ContainerNode"' in body and 'uri="vos://example.org~cavern/home/alice/new"' in body

    def test_mkdir_existing(self, rw, srcnet):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT), ("PUT", "/nodes/home/alice/new"): _resp(409),
                           ("GET", "/nodes/home/alice/new"): _resp(200, _node("home/alice/new", "vos:ContainerNode"))})
        srcnet.session.request.side_effect = fake
        with pytest.raises(FileExistsError):
            rw.mkdir("home/alice/new")
        assert rw.mkdir("home/alice/new", exist_ok=True) == "home/alice/new"

    def test_mkdir_exist_ok_but_is_a_file(self, rw, srcnet):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT), ("PUT", "/nodes/home/alice/f"): _resp(409),
                           ("GET", "/nodes/home/alice/f"): _resp(200, _node("home/alice/f"))})
        srcnet.session.request.side_effect = fake
        with pytest.raises(FileExistsError):
            rw.mkdir("home/alice/f", exist_ok=True)

    def test_mkdir_parents_creates_missing_ones(self, rw, srcnet):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                           ("GET", "/nodes/home"): _resp(200, _node("home", "vos:ContainerNode")),
                           ("GET", "/nodes/home/alice"): _resp(200, _node("home/alice", "vos:ContainerNode")),
                           ("PUT", "/nodes/home/alice/a"): _resp(201), ("PUT", "/nodes/home/alice/a/b"): _resp(201)})
        srcnet.session.request.side_effect = fake
        rw.mkdir("home/alice/a/b", parents=True)
        puts = [c[1].replace(BASE, "") for c in fake.calls if c[0] == "PUT"]
        assert puts == ["/nodes/home/alice/a", "/nodes/home/alice/a/b"]

    def test_put_negotiates_then_uploads(self, rw, srcnet, tmp_path):
        local = tmp_path / "a.txt"
        local.write_text("hello")
        transfer = ('<vos:transfer xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0"><vos:protocol uri="x">'
                    '<vos:endpoint>https://data.example.org/put/abc</vos:endpoint></vos:protocol></vos:transfer>')
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                           ("POST", "/synctrans"): _redirect(BASE + "/xfer/1"),
                           ("GET", "/xfer/1"): _resp(200, transfer)})
        srcnet.session.request.side_effect = fake
        srcnet.session.put.return_value = _resp(201)
        assert rw.put(str(local), "home/alice/a.txt") == "home/alice/a.txt"
        negotiation = [c for c in fake.calls if c[0] == "POST"][0][2]["data"].decode()
        assert "<vos:direction>pushToVoSpace</vos:direction>" in negotiation
        assert "vos://example.org~cavern/home/alice/a.txt" in negotiation
        assert "ivo://ivoa.net/sso#token" in negotiation
        assert srcnet.session.put.call_args.args[0] == "https://data.example.org/put/abc"

    def test_put_refuses_existing_without_overwrite(self, rw, srcnet, tmp_path):
        local = tmp_path / "a.txt"
        local.write_text("hello")
        existing = _resp(200, _node("home/alice/a.txt"))
        srcnet.session.request.side_effect = FakeCavern({("GET", "/nodes/home/alice/a.txt"): existing})
        with pytest.raises(FileExistsError, match="overwrite=True"):
            rw.put(str(local), "home/alice/a.txt")

    def test_put_missing_local_file(self, rw):
        with pytest.raises(FileNotFoundError, match="local file"):
            rw.put("/no/such/file", "home/alice/a.txt")

    def test_put_reports_endpoint_failures(self, rw, srcnet, tmp_path):
        local = tmp_path / "a.txt"
        local.write_text("hello")
        transfer = ('<vos:transfer xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0"><vos:protocol uri="x">'
                    '<vos:endpoint>https://data.example.org/put/abc</vos:endpoint></vos:protocol></vos:transfer>')
        srcnet.session.request.side_effect = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                                                         ("POST", "/synctrans"): _resp(200, transfer)})
        srcnet.session.put.return_value = _resp(507, "quota exceeded")
        with pytest.raises(OSError, match="507 quota exceeded"):
            rw.put(str(local), "home/alice/a.txt", overwrite=True)

    def test_delete_file(self, rw, srcnet):
        fake = FakeCavern({("DELETE", "/nodes/home/alice/a.txt"): _resp(204)})
        srcnet.session.request.side_effect = fake
        assert rw.delete("home/alice/a.txt") == "home/alice/a.txt"

    @pytest.mark.parametrize("answer", [_resp(400, "container node 'x' is not empty"), _resp(409)])
    def test_delete_non_empty_folder(self, rw, srcnet, answer):
        srcnet.session.request.side_effect = FakeCavern({("DELETE", "/nodes/home/alice/x"): answer})
        with pytest.raises(OSError, match="recursive=True"):
            rw.delete("home/alice/x")

    @pytest.mark.parametrize("path", ["home", "home/alice", "/home/alice/"])
    def test_delete_refuses_home_and_above(self, rw, srcnet, path):
        with pytest.raises(ValueError, match="Refusing"):
            rw.delete(path)
        srcnet.session.request.assert_not_called()

    def test_delete_recursive_runs_job(self, rw, srcnet):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                           ("GET", "/nodes/home/alice/x"): _resp(200, _node("home/alice/x", "vos:ContainerNode")),
                           ("POST", "/async-delete"): _redirect(BASE + "/async-delete/j1"),
                           ("POST", "/async-delete/j1/phase"): _redirect(BASE + "/async-delete/j1"),
                           ("GET", "/async-delete/j1"): _resp(200, _job("COMPLETED"))})
        srcnet.session.request.side_effect = fake
        rw.delete("home/alice/x", recursive=True)
        job_post = [c for c in fake.calls if c[1].endswith("/async-delete")][0]
        assert job_post[2]["data"] == {"target": "vos://example.org~cavern/home/alice/x"}

    def test_job_error_message(self, rw, srcnet):
        fake = FakeCavern({("POST", "/j/phase"): _resp(200),
                           ("GET", "/j"): _resp(200, _job("ERROR", "NodeNotFound: /home/alice/x"))})
        srcnet.session.request.side_effect = fake
        with pytest.raises(OSError, match="NodeNotFound"):
            rw._run_job(BASE + "/j", timeout=5)

    def test_job_timeout(self, rw, srcnet):
        srcnet.session.request.side_effect = FakeCavern({("POST", "/j/phase"): _resp(200),
                                                         ("GET", "/j"): _resp(200, _job("EXECUTING"))})
        with patch("astroquery.srcnet.user_storage.time.monotonic", side_effect=[0, 0, 100]):
            with pytest.raises(TimeoutError):
                rw._run_job(BASE + "/j", timeout=10)

    def _move_fake(self, dest_answer):
        return FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                           ("GET", "/nodes/home/alice/dest"): dest_answer,
                           ("POST", "/transfers"): _redirect(BASE + "/transfers/t1"),
                           ("POST", "/transfers/t1/phase"): _redirect(BASE + "/transfers/t1"),
                           ("GET", "/transfers/t1"): _resp(200, _job("COMPLETED"))})

    def test_move_into_folder(self, rw, srcnet):
        fake = self._move_fake(_resp(200, _node("home/alice/dest", "vos:ContainerNode")))
        srcnet.session.request.side_effect = fake
        assert rw.move("home/alice/a.txt", "home/alice/dest") == "home/alice/dest/a.txt"
        body = [c for c in fake.calls if c[1].endswith("/transfers")][0][2]["data"].decode()
        assert "<vos:target>vos://example.org~cavern/home/alice/a.txt</vos:target>" in body
        assert "<vos:direction>vos://example.org~cavern/home/alice/dest</vos:direction>" in body
        assert "<vos:keepBytes>false</vos:keepBytes>" in body

    def test_move_rename(self, rw, srcnet):
        srcnet.session.request.side_effect = self._move_fake(_resp(404, "NodeNotFound"))
        assert rw.move("home/alice/a.txt", "home/alice/dest") == "home/alice/dest"

    def test_move_onto_existing_file_refused(self, rw, srcnet):
        srcnet.session.request.side_effect = self._move_fake(_resp(200, _node("home/alice/dest")))
        with pytest.raises(FileExistsError):
            rw.move("home/alice/a.txt", "home/alice/dest")

    @pytest.mark.parametrize("public, value", [(True, "true"), (False, "false")])
    def test_set_public(self, rw, srcnet, public, value):
        fake = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                           ("GET", "/nodes/home/alice/a.txt"): _resp(200, _node("home/alice/a.txt", public=value)),
                           ("POST", "/nodes/home/alice/a.txt"): _resp(200)})
        srcnet.session.request.side_effect = fake
        assert rw.set_public("home/alice/a.txt", public)["is_public"] is public
        body = [c for c in fake.calls if c[0] == "POST"][0][2]["data"].decode()
        assert 'xsi:type="vos:DataNode"' in body
        assert f'uri="ivo://ivoa.net/vospace/core#ispublic" readOnly="false">{value}<' in body

    def test_other_errors_carry_the_service_message(self, rw, srcnet):
        srcnet.session.request.return_value = _resp(500, "disk full")
        with pytest.raises(OSError, match="500 .*disk full"):
            rw.info("home/alice/a.txt")


class TestReviewFixes:
    """Greptile on !28: special characters, token forwarding, job deadlines, upload fallback, redaction."""

    def test_uri_paths_are_decoded(self):
        assert _node_path("vos://example.org~cavern/home/alice/my%20file%231.txt") == "home/alice/my file#1.txt"

    def test_plain_paths_are_literal(self):
        assert _node_path("home/alice/100%25.txt") == "home/alice/100%25.txt"

    def test_encoded_slash_in_uri_refused(self):
        with pytest.raises(ValueError):
            _node_path("vos://example.org~cavern/home/alice/a%2Fb")

    def test_listed_path_round_trips(self, us, srcnet, tmp_path):
        listing = LISTING.replace("home/alice/cube.fits", "home/alice/my%20cube%231.fits")
        srcnet.session.request.return_value = _resp(200, listing)
        row = us.list("home/alice")[1]
        assert row["name"] == "my cube#1.fits" and row["path"] == "home/alice/my cube#1.fits"
        srcnet.session.request.return_value = _resp(200, chunks=[b"x"])
        us.get(row["path"], str(tmp_path / "out.fits"))
        assert srcnet.session.request.call_args.args[1] == BASE + "/files/home/alice/my%20cube%231.fits"

    def test_vos_uri_encodes_segments(self, rw):
        rw._vos_root_cache = "vos://example.org~cavern"
        assert rw._vos_uri("home/alice/my file#1.txt") == "vos://example.org~cavern/home/alice/my%20file%231.txt"

    def _upload(self, rw, srcnet, tmp_path, protocols):
        local = tmp_path / "a.txt"
        local.write_text("hello")
        transfer = f'<vos:transfer xmlns:vos="http://www.ivoa.net/xml/VOSpace/v2.0">{protocols}</vos:transfer>'
        srcnet.session.request.side_effect = FakeCavern({("GET", "/nodes/"): _resp(200, ROOT),
                                                         ("POST", "/synctrans"): _resp(200, transfer)})
        return str(local)

    def test_preauthorised_endpoint_gets_no_token(self, rw, srcnet, tmp_path):
        local = self._upload(rw, srcnet, tmp_path,
                             '<vos:protocol uri="p"><vos:endpoint>https://s.org/preauth:xyz/a</vos:endpoint>'
                             '</vos:protocol>')
        srcnet.session.put.return_value = _resp(201)
        rw.put(local, "home/alice/a.txt", overwrite=True)
        assert srcnet.session.put.call_args.kwargs["headers"] == {"Authorization": None}

    def test_token_endpoint_gets_token(self, rw, srcnet, tmp_path):
        local = self._upload(rw, srcnet, tmp_path,
                             '<vos:protocol uri="p"><vos:securityMethod uri="ivo://ivoa.net/sso#token"/>'
                             '<vos:endpoint>https://s.org/files/a</vos:endpoint></vos:protocol>')
        srcnet.session.put.return_value = _resp(201)
        rw.put(local, "home/alice/a.txt", overwrite=True)
        assert srcnet.session.put.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}

    def test_non_https_endpoint_skipped(self, rw, srcnet, tmp_path):
        local = self._upload(rw, srcnet, tmp_path,
                             '<vos:protocol uri="p"><vos:endpoint>http://s.org/a</vos:endpoint></vos:protocol>')
        with pytest.raises(OSError, match="not HTTPS"):
            rw.put(local, "home/alice/a.txt", overwrite=True)
        srcnet.session.put.assert_not_called()

    def test_network_error_falls_back_to_next_endpoint(self, rw, srcnet, tmp_path):
        import requests
        local = self._upload(rw, srcnet, tmp_path,
                             '<vos:protocol uri="p"><vos:endpoint>https://down.org/a</vos:endpoint>'
                             '<vos:endpoint>https://up.org/a</vos:endpoint></vos:protocol>')
        srcnet.session.put.side_effect = [requests.exceptions.ConnectionError("boom"), _resp(201)]
        assert rw.put(local, "home/alice/a.txt", overwrite=True) == "home/alice/a.txt"
        assert srcnet.session.put.call_args.args[0] == "https://up.org/a"

    def test_upload_errors_name_hosts_not_urls(self, rw, srcnet, tmp_path):
        local = self._upload(rw, srcnet, tmp_path,
                             '<vos:protocol uri="p"><vos:endpoint>https://s.org/preauth:SECRET/a</vos:endpoint>'
                             '</vos:protocol>')
        srcnet.session.put.return_value = _resp(500, "token=abc123 failed")
        with pytest.raises(OSError) as err:
            rw.put(local, "home/alice/a.txt", overwrite=True)
        assert "SECRET" not in str(err.value) and "abc123" not in str(err.value) and "s.org" in str(err.value)

    def test_service_messages_redacted(self, rw, srcnet):
        srcnet.session.request.return_value = _resp(500, "failed for Bearer eyJabc.def.ghi")
        with pytest.raises(OSError) as err:
            rw.info("home/alice/a.txt")
        assert "eyJabc" not in str(err.value)

    def test_job_deadline_checked_before_polling(self, rw, srcnet):
        srcnet.session.request.side_effect = FakeCavern({("POST", "/j/phase"): _resp(200),
                                                         ("GET", "/j"): _resp(200, _job("EXECUTING"))})
        with patch("astroquery.srcnet.user_storage.time.monotonic", side_effect=[0, 0, 5]):
            with pytest.raises(TimeoutError):
                rw._run_job(BASE + "/j", timeout=1)
        # started the job, but never polled once the time was up
        assert [c[0] for c in srcnet.session.request.side_effect.calls] == ["POST"]

    def test_job_requests_bounded_by_time_left(self, rw, srcnet):
        fake = FakeCavern({("POST", "/j/phase"): _resp(200), ("GET", "/j"): _resp(200, _job("COMPLETED"))})
        srcnet.session.request.side_effect = fake
        with patch("astroquery.srcnet.user_storage.time.monotonic", side_effect=[0, 0, 7, 7]):
            rw._run_job(BASE + "/j", timeout=10)
        poll = [c for c in fake.calls if c[0] == "GET"][0][2]
        assert poll["params"] == {"WAIT": 3} and poll["timeout"][1] <= 4

    def test_request_timeout_becomes_timeout_error(self, rw, srcnet):
        import requests
        srcnet.session.request.side_effect = requests.exceptions.ReadTimeout("read timed out")
        with pytest.raises(TimeoutError, match="not finished"):
            rw._run_job(BASE + "/j", timeout=1)

    @pytest.mark.parametrize("name", ["a b.txt", "a#b.txt", "a%b.txt", "a?b.txt", "café.txt", "tab\tname"])
    def test_names_cavern_cannot_store_are_refused(self, rw, srcnet, tmp_path, name):
        local = tmp_path / "a.txt"
        local.write_text("x")
        for call in (lambda: rw.put(str(local), f"home/alice/{name}"),
                     lambda: rw.mkdir(f"home/alice/{name}"),
                     lambda: rw.mkdir(f"home/alice/{name}/sub", parents=True)):
            with pytest.raises(ValueError, match="can't store"):
                call()
        srcnet.session.request.assert_not_called()

    def test_move_to_unsupported_name_refused(self, rw, srcnet):
        srcnet.session.request.side_effect = FakeCavern({("GET", "/nodes/home/alice/a b.txt"): _resp(404)})
        with pytest.raises(ValueError, match="can't store"):
            rw.move("home/alice/a.txt", "home/alice/a b.txt")

    @pytest.mark.parametrize("name", ["a+b.txt", "a(b).txt", "a,b.txt", "a&b.txt", "a:b.txt", "a~b.txt", "a@b.txt"])
    def test_names_that_work_are_allowed(self, name):
        from astroquery.srcnet.user_storage import _check_new_name
        _check_new_name(f"home/alice/{name}")

    def test_existing_odd_names_can_still_be_read(self, us, srcnet):
        srcnet.session.request.return_value = _resp(200, EMPTY)
        us.list("home/alice/old folder")  # no ValueError: only new names are checked
