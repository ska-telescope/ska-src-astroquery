"""
From the science-metadata catalogue to a download: untruncated catalogue IDs,
DataLink links, trying each site in turn, and the token refresh order.
"""
import io
import os
from unittest.mock import MagicMock, patch

import pytest
import requests
from astropy.io.votable import parse

from astroquery.srcnet.data_access import DOWNLOAD_TIMEOUT_S, DataAccessClass
from astroquery.srcnet.data_discovery import _unbound_text_columns

DM_API = "https://dm.example/api/v1"
DATALINK = "https://datalink.example/api/v1/links"
OBS_ID = "ivo://skao.int/sv?srcnet-sd-comm/" + "eb-dsc-065-" + "x" * 80 + "-obs/eb-dsc-065-" + "x" * 80 + "-eb"


def _votable(value, arraysize='128*', datatype="char"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results"><TABLE>'
        f'<FIELD name="obs_id" datatype="{datatype}" arraysize="{arraysize}"/>'
        '<FIELD name="n" datatype="int"/><FIELD name="code" datatype="char" arraysize="4"/>'
        f'<DATA><TABLEDATA><TR><TD>{value}</TD><TD>3</TD><TD>abcd</TD></TR></TABLEDATA></DATA>'
        '</TABLE></RESOURCE></VOTABLE>'
    ).encode()


def _hooked_response(body, content_type):
    """A requests.Response as pyvo gets it (streamed), passed through the hook."""
    session = requests.Session()
    _unbound_text_columns(session)
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = content_type
    response.raw = io.BytesIO(body)
    for hook in session.hooks["response"]:
        hook(response)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue IDs are not truncated
# ─────────────────────────────────────────────────────────────────────────────

class TestUnboundTextColumns:

    def test_a_long_obs_id_survives_parsing(self):
        """Argus declares obs_id as 128* but returns longer IDs; astropy would cut them at 128."""
        assert len(OBS_ID) > 128
        response = _hooked_response(_votable(OBS_ID), "application/x-votable+xml")

        table = parse(response.raw).get_first_table().to_table()

        assert table["obs_id"][0] == OBS_ID

    def test_without_the_hook_astropy_truncates(self):
        """The failure the hook prevents."""
        table = parse(io.BytesIO(_votable(OBS_ID))).get_first_table().to_table()
        assert len(table["obs_id"][0]) == 128

    def test_only_bounded_text_sizes_change(self):
        """Fixed-size text and non-text fields keep their declarations."""
        response = _hooked_response(_votable("short", arraysize="16*"), "text/xml")
        body = response.content

        assert b'name="obs_id" datatype="char" arraysize="*"' in body
        assert b'name="code" datatype="char" arraysize="4"' in body
        assert b'name="n" datatype="int"' in body

    def test_unicode_char_fields_are_unbounded_too(self):
        response = _hooked_response(_votable(OBS_ID, datatype="unicodeChar"), "text/xml")
        assert b'datatype="unicodeChar" arraysize="*"' in response.content

    def test_other_xml_stays_readable_as_a_stream(self):
        """A UWS job document is passed through, and pyvo can still read the body with decode_content."""
        job = b'<?xml version="1.0"?><uws:job xmlns:uws="http://www.ivoa.net/xml/UWS/v1.0"/>'
        response = _hooked_response(job, "text/xml")

        assert response.raw.read(decode_content=True) == job

    def test_non_xml_responses_are_untouched(self):
        raw = io.BytesIO(b"obs_id\n" + OBS_ID.encode())
        session = requests.Session()
        _unbound_text_columns(session)
        response = requests.Response()
        response.headers["Content-Type"] = "text/csv"
        response.raw = raw
        session.hooks["response"][0](response)

        assert response.raw is raw


# ─────────────────────────────────────────────────────────────────────────────
# DataLink links of a catalogue ID
# ─────────────────────────────────────────────────────────────────────────────

def _links_votable(rows):
    """A DataLink response as the SRCNet DataLink renders it."""
    trs = "".join(
        f"<TR><TD>{row_id}</TD><TD>{url}</TD><TD></TD><TD></TD><TD>{sem}</TD><TD></TD><TD></TD><TD></TD><TD></TD></TR>"
        for row_id, url, sem in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results"><TABLE>'
        '<FIELD name="ID" datatype="char" arraysize="*" ucd="meta.id;meta.main"/>'
        '<FIELD name="access_url" datatype="char" arraysize="*" ucd="meta.ref.url"/>'
        '<FIELD name="service_def" datatype="char" arraysize="*" ucd="meta.ref"/>'
        '<FIELD name="error_message" datatype="char" arraysize="*" ucd="meta.code.error"/>'
        '<FIELD name="semantics" datatype="char" arraysize="*" ucd="meta.code"/>'
        '<FIELD name="description" datatype="char" arraysize="*" ucd="meta.note"/>'
        '<FIELD name="content_type" datatype="char" arraysize="*" ucd="meta.code.mime"/>'
        '<FIELD name="content_length" datatype="long" unit="byte" ucd="phys.size;meta.file"/>'
        '<FIELD name="content_qualifier" datatype="char" arraysize="*" ucd="meta.code.class"/>'
        f'<DATA><TABLEDATA>{trs}</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>'
    ).encode()


@pytest.fixture
def da():
    srcnet = MagicMock(access_token=None, refresh_token=None, srcnet_dm_api_base_address=DM_API,
                       srcnet_datalink_service_url=DATALINK)
    return DataAccessClass(srcnet)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _ok(content=b"", json_data=None, chunks=()):
    resp = MagicMock(status_code=200, content=content)
    resp.json.return_value = json_data
    resp.iter_content.return_value = list(chunks)
    return resp


def _failing(status_code, detail="boom"):
    resp = MagicMock(status_code=status_code, text=detail)
    resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} {detail}", response=resp)
    return resp


class TestGetLinks:

    PRODUCT = "ivo://skao.int/sv?srcnet-sd-comm/obs/eb/SKAO-08DD3647FD"

    def test_a_product_lists_its_files_with_their_rucio_dids(self, da):
        da.session.get.return_value = _ok(_links_votable([
            ("ivo://skao.int?srcnet_sd.comm/eb.ms", "davs://a/srcnet_sd.comm/eb.ms/table.dat", "#this"),
            ("ivo://skao.int?srcnet_sd.comm/eb.ms/table.dat", "davs://a/srcnet_sd.comm/eb.ms/table.dat", "#child"),
            ("ivo://skao.int?srcnet_sd.comm/eb.ms/ANTENNA/table.f0", "davs://b/srcnet_sd.comm/eb.ms/ANTENNA/table.f0", "#child"),
        ]))

        links = da.get_links(self.PRODUCT)

        assert list(links["semantics"]) == ["#this", "#child", "#child"]
        assert list(links["rucio_did"]) == ["", "srcnet_sd.comm:eb.ms/table.dat", "srcnet_sd.comm:eb.ms/ANTENNA/table.f0"]

    def test_the_request_accepts_any_replica_and_works_with_older_datalink(self, da):
        """Lowercase id (all DataLink versions read it) and no required colocated service."""
        da.session.get.return_value = _ok(_links_votable([("ivo://a?ns/f", "https://x/ns/f", "#this")]))

        da.get_links("  " + self.PRODUCT + " ")

        url = da.session.get.call_args.args[0]
        assert url.startswith(DATALINK + "?id=ivo%3A%2F%2Fskao.int%2Fsv%3Fsrcnet-sd-comm")
        assert "colocated_services=&" in url and "sort=random" in url

    def test_a_single_file_has_its_did_on_the_this_row(self, da):
        da.session.get.return_value = _ok(_links_votable([
            ("ivo://skao.int?srcnet_sd.comm/eb.ms/table.dat", "davs://a/srcnet_sd.comm/eb.ms/table.dat", "#this"),
        ]))

        assert list(da.get_links("skao:obs/eb.prod/eb.ms/table.dat")["rucio_did"]) == ["srcnet_sd.comm:eb.ms/table.dat"]

    def test_a_datalink_error_reports_its_detail(self, da):
        da.session.get.return_value = _failing(404, '{"detail":"No data found for id"}')

        with pytest.raises(Exception, match="No data found"):
            da.get_links(self.PRODUCT)

    def test_an_empty_identifier_is_refused_before_any_request(self, da):
        with pytest.raises(ValueError, match="identifier"):
            da.get_links("  ")
        da.session.get.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# get_data tries each site in turn
# ─────────────────────────────────────────────────────────────────────────────

SITES = [
    {"identifier": "SITE_A", "replicas": ["davs://a.example/ns/f.dat"], "associated_storage_area_id": "area-a"},
    {"identifier": "SITE_B", "replicas": ["davs://b.example/ns/f.dat"], "associated_storage_area_id": "area-b"},
]


class TestGetDataTriesEachSite:

    def test_a_storage_token_error_moves_on_to_the_next_site(self, da, tmp_path):
        da.session.get.side_effect = [_ok(json_data=SITES), _failing(500), _ok(json_data={"access_token": "t"})]
        output = str(tmp_path / "f.dat")

        with patch("requests.get", return_value=_ok(chunks=[b"data"])) as storage_get:
            assert da.get_data("ns", "f.dat", output_file=output) == output

        assert storage_get.call_args.args[0] == "https://b.example/ns/f.dat"
        assert storage_get.call_args.kwargs["timeout"] == DOWNLOAD_TIMEOUT_S
        assert _read(output) == b"data"

    def test_an_unreachable_storage_moves_on_to_the_next_site(self, da, tmp_path):
        da.session.get.side_effect = [_ok(json_data=SITES), _ok(json_data={"access_token": "t"}), _ok(json_data={"access_token": "t"})]
        output = str(tmp_path / "f.dat")

        with patch("requests.get", side_effect=[requests.exceptions.ConnectTimeout("a.example"), _ok(chunks=[b"data"])]):
            assert da.get_data("ns", "f.dat", output_file=output) == output

        assert _read(output) == b"data"

    def test_when_every_site_fails_the_error_names_each_one(self, da, tmp_path):
        da.session.get.side_effect = [_ok(json_data=SITES), _failing(500, "IndexError"), _ok(json_data={"access_token": "t"})]
        output = str(tmp_path / "f.dat")

        with patch("requests.get", side_effect=requests.exceptions.SSLError("bad certificate")):
            with pytest.raises(Exception, match="No site could serve ns:f.dat") as info:
                da.get_data("ns", "f.dat", output_file=output)

        assert "SITE_A" in str(info.value) and "SITE_B" in str(info.value)
        assert not os.path.exists(output) and not os.path.exists(output + ".part")

    def test_a_download_failing_midway_leaves_no_file(self, da, tmp_path):
        """Chunks already written are discarded: no partial or final file is left behind."""
        def chunks_then_break(**_):
            yield b"first chunk"
            raise requests.exceptions.ChunkedEncodingError("cut")

        broken = _ok()
        broken.iter_content.side_effect = chunks_then_break
        da.session.get.side_effect = [_ok(json_data=SITES[:1]), _ok(json_data={"access_token": "t"})]
        output = str(tmp_path / "f.dat")

        with patch("requests.get", return_value=broken):
            with pytest.raises(Exception, match="No site could serve"):
                da.get_data("ns", "f.dat", output_file=output)

        assert os.listdir(tmp_path) == []

    def test_an_existing_file_is_untouched_until_a_download_succeeds(self, da, tmp_path):
        """Neither the destination nor a file named like the old fixed partial name is overwritten by a failure."""
        output = tmp_path / "f.dat"
        output.write_bytes(b"previous")
        lookalike = tmp_path / "f.dat.part"
        lookalike.write_bytes(b"someone else's")
        da.session.get.side_effect = [_ok(json_data=SITES[:1]), _ok(json_data={"access_token": "t"})]

        with patch("requests.get", side_effect=requests.exceptions.ConnectTimeout("a.example")):
            with pytest.raises(Exception):
                da.get_data("ns", "f.dat", output_file=str(output))

        assert output.read_bytes() == b"previous"
        assert lookalike.read_bytes() == b"someone else's"
        assert sorted(os.listdir(tmp_path)) == ["f.dat", "f.dat.part"]

    def test_a_downloaded_file_gets_the_usual_permissions(self, da, tmp_path):
        """The result follows the umask like any new file."""
        da.session.get.side_effect = [_ok(json_data=SITES[:1]), _ok(json_data={"access_token": "t"})]
        output = tmp_path / "f.dat"
        mask = os.umask(0o022)
        try:
            with patch("requests.get", return_value=_ok(chunks=[b"data"])):
                da.get_data("ns", "f.dat", output_file=str(output))
        finally:
            os.umask(mask)

        assert oct(output.stat().st_mode & 0o777) == oct(0o644)

    def test_the_umask_is_never_changed(self, da, tmp_path):
        """Changing the process-wide umask, even briefly, could expose files other threads create meanwhile."""
        da.session.get.side_effect = [_ok(json_data=SITES[:1]), _ok(json_data={"access_token": "t"})]
        with patch("os.umask", side_effect=AssertionError("umask changed")):
            with patch("requests.get", return_value=_ok(chunks=[b"data"])):
                da.get_data("ns", "f.dat", output_file=str(tmp_path / "f.dat"))

    def test_a_long_destination_name_still_downloads(self, da, tmp_path):
        """The temporary name does not grow with the destination's, so a name near the filesystem limit works."""
        name = "x" * 250 + ".dat"
        da.session.get.side_effect = [_ok(json_data=SITES[:1]), _ok(json_data={"access_token": "t"})]

        with patch("requests.get", return_value=_ok(chunks=[b"data"])):
            assert da.get_data("ns", name, output_file=str(tmp_path / name)).endswith(name)


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh happens before the exchange
# ─────────────────────────────────────────────────────────────────────────────

def test_an_expired_token_is_refreshed_before_it_is_exchanged():
    """The first call after expiry used to exchange (or skip) on the stale token and get a 401."""
    srcnet = MagicMock(access_token="old", refresh_token="refresh", srcnet_dm_api_base_address=DM_API,
                       srcnet_authn_api_address="https://authn.example/api/v1")
    # refresh_token_if_expired reads the expiry twice, then exchange_token_for_service reads the audience
    srcnet._decode_access_token.side_effect = [
        {"aud": "data-management-api", "exp": 1}, {"aud": "data-management-api", "exp": 1}, {"aud": "authn-api"},
    ]
    srcnet.session.get.side_effect = [
        _ok(json_data={"access_token": "fresh", "refresh_token": "r2"}),
        _ok(json_data={"access_token": "dm", "refresh_token": "r3"}),
        _ok(json_data={"bytes": 1}),
    ]
    da = DataAccessClass(srcnet)

    assert da.get_metadata("ns", "f.dat", plugin="DID_COLUMN") == {"bytes": 1}

    urls = [call.args[0] for call in srcnet.session.get.call_args_list]
    assert urls == [
        "https://authn.example/api/v1/refresh",
        "https://authn.example/api/v1/token/exchange/data-management-api",
        DM_API + "/metadata/ns/f.dat",
    ]
