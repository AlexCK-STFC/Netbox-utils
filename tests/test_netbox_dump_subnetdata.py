"""
Test cases for netbox2aquilon
"""

# pylint: disable=protected-access,missing-function-docstring

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from netbox_utils.netbox_dump_subnetdata import NetboxDumpSubnetdata


@pytest.fixture
def mock_dump_subnet_data(mocker):
    mocker.patch("os.path.isfile", return_value=True)

    def mock_read(instance, filenames, **kwargs):
        instance.read_dict({"netbox": {"url": "http://test", "token": "xyz"}})
        return filenames

    mocker.patch("configparser.ConfigParser.read", side_effect=mock_read, autospec=True)

    mock_obj = NetboxDumpSubnetdata()

    mocker.patch.object(mock_obj, "_validate_config")

    return mock_obj


def test__get_subnet_fields(mocker, make_nb_obj, mock_dump_subnet_data):
    mock_dump_subnet_data.netbox.ipam.prefixes = SimpleNamespace()
    mock_dump_subnet_data.netbox.ipam.prefixes.filter = mocker.MagicMock(
        return_value=make_nb_obj("PREFIXES_IPV4", index=...)
    )

    subnets = mock_dump_subnet_data._get_subnet_fields()
    assert len(subnets) == 5
    print(subnets)
    assert {s["SubnetAddress"] for s in subnets} == {
        "10.246.176.0",
        "172.16.254.0",
        "192.168.176.0",
        "192.168.216.64",
        "192.168.80.0",
    }


@pytest.mark.parametrize("method", ["txt", "json"])
def test_write_subnetdata_output(mocker, make_nb_obj, mock_dump_subnet_data, method):
    mock_dump_subnet_data.netbox.ipam.prefixes = SimpleNamespace()
    mock_dump_subnet_data.netbox.ipam.prefixes.filter = mocker.MagicMock(
        return_value=make_nb_obj("PREFIXES_IPV4", index=...)
    )

    # Load expected data for comparison
    data_dir = Path(__file__).parent / "testdata"
    file_path = data_dir / f"subnetdata.{method}"

    mock_file = mocker.mock_open()
    mocker.patch("netbox_utils.netbox_dump_subnetdata.open", mock_file)

    if method == "txt":
        with open(file_path, encoding="utf-8") as f:
            expected = f.readlines()
        mock_dump_subnet_data.write_subnetdata_txt("/tmp/fake_place")
        handle = mock_file()
        handle.writelines.assert_called_once_with(expected)
    else:
        with open(file_path, encoding="utf-8") as f:
            expected = json.load(f)
        json_dump_mock = mocker.patch.object(json, "dump")
        mock_dump_subnet_data.write_subnetdata_json("/tmp/fake_place")
        handle = mock_file()
        json_dump_mock.assert_called_with(expected, handle)

    mock_file.assert_any_call(
        f"/tmp/fake_place/subnetdata.{method}", "w", encoding="utf-8"
    )
