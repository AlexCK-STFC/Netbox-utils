"""
Test cases for core library
"""

import os

# pylint: disable=protected-access, redefined-outer-name
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pynetbox.models.dcim import Interfaces, Racks
from pynetbox.models.ipam import IpAddresses

from netbox_utils.scd_netbox import (
    NetboxInvalidError,
    NetboxMultipleResultsError,
    NetboxNotFoundError,
    NetboxUnmatchedIPError,
    SCDNetbox,
)


# ==========================================
# Fixtures
# ==========================================
@pytest.fixture
def fake_netbox(mocker):
    """Return a fake pynetbox-like API that SCDNetbox instance will use."""
    netbox = SimpleNamespace()

    netbox.dcim = SimpleNamespace(
        devices=SimpleNamespace(get=mocker.Mock(), filter=mocker.Mock()),
        interfaces=SimpleNamespace(filter=mocker.Mock()),
    )

    netbox.virtualization = SimpleNamespace(
        virtual_machines=SimpleNamespace(get=mocker.Mock()),
        interfaces=SimpleNamespace(filter=mocker.Mock()),
        virtual_disks=SimpleNamespace(filter=mocker.Mock()),
    )

    netbox.ipam = SimpleNamespace(
        ip_addresses=SimpleNamespace(get=mocker.Mock(), filter=mocker.Mock())
    )

    return netbox


@pytest.fixture
def scd(fake_netbox):
    """Return a configured SCDNetbox instance connected to our fake API."""
    config = {
        "netbox": {"url": "http://localhost", "token": "abc", "cert_path": "False"}
    }
    obj = SCDNetbox(config=config)
    obj.netbox = fake_netbox  # override pynetbox.api()
    return obj


# ==========================================
# Configuration & Initialization Tests
# ==========================================


class TestConfiguration:
    @pytest.mark.parametrize("cert_path", ["/my/cert", None])
    def test_init_from_connection(self, cert_path):
        """Test simple constructor from connection."""
        scd = SCDNetbox.from_connection(
            url="http://test", token="xyz", cert_path=cert_path
        )
        assert scd.config["netbox"]["url"] == "http://test"
        assert scd.netbox.http_session.verify == (cert_path or True)

    def test_init_deprecated_arg(self, mocker):
        """Test legacy kwarg backwards-compatibility and deprecation warning."""
        # Mocking filesystem and config load to prevent KeyError
        mocker.patch("os.path.isfile", return_value=True)

        def mock_read(self, filenames, encoding=None):
            self.read_dict({"netbox": {"url": "http://test", "token": "xyz"}})

        mocker.patch("configparser.ConfigParser.read", mock_read)

        with pytest.warns(
            DeprecationWarning, match="use 'additional_config_name' instead"
        ):
            SCDNetbox(config="aquilon", additonal_config_name="legacy")

    def test_init_conflicting_kwargs(self):
        """Test passing both old and new kwargs raises an error."""
        with pytest.raises(ValueError, match="Pass only 'additional_config_name'"):
            SCDNetbox(
                config="aquilon",
                additional_config_name="new",
                additonal_config_name="old",
            )

    def test_missing_config_section(self):
        with pytest.raises(
            ValueError, match="Missing required config section: \\[netbox\\]"
        ):
            SCDNetbox(config={"other": {"url": "foo"}})

    def test_missing_config_keys(self):
        with pytest.raises(
            ValueError,
            match="Missing required keys in section \\[netbox\\]: \\['token'\\]",
        ):
            SCDNetbox(config={"netbox": {"url": "http://test"}})

    @pytest.mark.parametrize("additional_config_name", ["extra", None])
    def test_load_config_aquilon(self, mocker, additional_config_name):
        mocker.patch("os.path.isfile", return_value=True)

        def mock_read(instance, filenames, **kwargs):
            instance.read_dict({"netbox": {"url": "http://test", "token": "xyz"}})
            return filenames

        read_mock = mocker.patch(
            "configparser.ConfigParser.read", side_effect=mock_read, autospec=True
        )

        mocker.patch.object(SCDNetbox, "_validate_config")

        SCDNetbox(config="aquilon", additional_config_name=additional_config_name)

        # Use mocker.ANY to account for the 'self' argument recorded by autospec
        read_mock.assert_any_call(
            mocker.ANY,
            [
                "/var/quattor/etc/scd_netbox.cfg",
                os.path.expanduser("~/.scd_netbox.cfg"),
            ],
        )

        if additional_config_name:
            read_mock.assert_any_call(
                mocker.ANY,
                [
                    "/var/quattor/etc/extra.cfg",
                    os.path.expanduser("~/.extra.cfg"),
                ],
            )
            assert read_mock.call_count == 2
        else:
            assert read_mock.call_count == 1

    def test_missing_aquilon_config_files(self, mocker):
        mocker.patch("os.path.isfile", return_value=False)
        with pytest.raises(
            ValueError,
            match="""Using 'aquilon' config option, but scd_netbox.cfg not found in expected paths."
                Are you running this script on the Aquilon broker,
                or should you use a different config option?""",
        ):
            SCDNetbox(config="aquilon")

    def test_load_config_file_path(self, mocker, tmp_path):
        """Test passing a single valid file path."""
        cfg_file = tmp_path / "test.cfg"
        cfg_file.write_text("[netbox]\nurl=http://a\ntoken=b\n")

        scd = SCDNetbox(config=str(cfg_file))
        assert scd.config["netbox"]["url"] == "http://a"

    def test_load_config_iterable_paths(self, mocker, tmp_path):
        """Test passing an iterable sequence of file paths."""
        cfg_file = tmp_path / "test.cfg"
        cfg_file.write_text("[netbox]\nurl=http://a\ntoken=b\n")

        # Ensure os.path.isfile returns True for our mock file
        mocker.patch("os.path.isfile", side_effect=lambda x: str(x) == str(cfg_file))

        scd = SCDNetbox(config=[str(cfg_file)])
        assert scd.config["netbox"]["url"] == "http://a"

    def test_load_config_paths_not_found(self, mocker):
        """Test iterable sequence of paths where none exist."""
        mocker.patch("os.path.isfile", return_value=False)
        with pytest.raises(ValueError):
            SCDNetbox(config=["/fake/path.cfg"])

    def test_load_config_invalid_type(self):
        with pytest.raises(ValueError, match="config must be one of"):
            SCDNetbox(config=123)  # type: ignore


# ==========================================
# Core Retrieval Tests
# ==========================================


class TestHostRetrieval:
    @pytest.mark.parametrize(
        "has_dev, has_vm, expected_exc, expected_return",
        [
            (True, False, None, "device"),
            (False, True, None, "vm"),
            (True, True, NetboxInvalidError, None),
            (False, False, NetboxNotFoundError, None),
        ],
    )
    def test_get_host(
        self, scd, make_nb_obj, has_dev, has_vm, expected_exc, expected_return
    ):
        """Exhaustive matrix of exact matches, misses, and ambiguity."""
        dev_mock = make_nb_obj("DEVICE_PHYSICAL") if has_dev else None
        vm_mock = make_nb_obj("DEVICE_VIRTUAL") if has_vm else None

        scd.netbox.dcim.devices.get.return_value = dev_mock
        scd.netbox.virtualization.virtual_machines.get.return_value = vm_mock

        if expected_exc:
            with pytest.raises(expected_exc):
                scd._get_host({}, {}, "test desc")
        else:
            result = scd._get_host({}, {}, "test desc")
            assert result == (dev_mock if expected_return == "device" else vm_mock)

    def test_get_host_multiple_results_device(self, scd):
        scd.netbox.dcim.devices.get.side_effect = ValueError("Multiple returned")
        with pytest.raises(NetboxMultipleResultsError, match="Multiple devices found"):
            scd._get_host({}, {}, "test desc")

    def test_get_host_multiple_results_vm(self, scd):
        scd.netbox.dcim.devices.get.return_value = None
        scd.netbox.virtualization.virtual_machines.get.side_effect = ValueError(
            "Multiple returned"
        )
        with pytest.raises(NetboxMultipleResultsError, match="Multiple VMs found"):
            scd._get_host({}, {}, "test desc")

    @pytest.mark.parametrize(
        "method_name, query_val, filter_key, desc",
        [
            ("get_device_by_magdb_id", "12345", "cf_magdb_system_id", "MagDB ID 12345"),
            ("get_device_by_name", "server01", "name", "name 'server01'"),
        ],
    )
    def test_get_device_by_attributes(
        self, scd, mocker, method_name, query_val, filter_key, desc
    ):
        mock_get_host = mocker.patch.object(scd, "_get_host", return_value="mock_host")
        method = getattr(scd, method_name)
        result = method(query_val)

        assert result == "mock_host"
        mock_get_host.assert_called_once_with(
            device_filter={filter_key: query_val},
            vm_filter={filter_key: query_val},
            desc=desc,
        )


# ==========================================
# IP & Interface Tests
# ==========================================


class TestNetworkResolution:
    def test_resolve_ip_to_host_string_not_found(self, scd):
        """Passing a string IP that doesn't exist in Netbox."""
        scd.netbox.ipam.ip_addresses.get.return_value = None
        with pytest.raises(NetboxNotFoundError):
            scd._resolve_ip_to_host("192.168.1.1")

    def test_resolve_ip_to_host_unassigned(self, scd, make_nb_obj):
        """IP exists but has no assigned interface."""
        ip = make_nb_obj("ADDRESSES_IPV4", assigned_object_type=None)
        with pytest.raises(NetboxUnmatchedIPError):
            scd._resolve_ip_to_host(ip)

    @pytest.mark.parametrize(
        "host_fixture, iface_attr, assigned_obj_type, string_ip",
        [
            ("DEVICE_PHYSICAL", "device", "dcim.interface", True),
            ("DEVICE_VIRTUAL", "virtual_machine", "virtualization.vminterface", False),
        ],
    )
    def test_resolve_ip_to_host_valid(
        self, scd, make_nb_obj, host_fixture, iface_attr, assigned_obj_type, string_ip
    ):
        """IP assigned to a physical device or VM interface."""
        host = make_nb_obj(host_fixture)
        iface = MagicMock(spec=Interfaces, **{iface_attr: host})
        ip = make_nb_obj(
            "ADDRESSES_IPV4",
            assigned_object_type=assigned_obj_type,
            assigned_object=iface,
        )
        if string_ip:
            scd.netbox.ipam.ip_addresses.get.return_value = ip
            ip_address = "123.123.123.123"
        else:
            ip_address = ip

        assert scd._resolve_ip_to_host(ip_address) == host

    def test_resolve_ip_to_host_invalid_type(self, scd, make_nb_obj):
        """IP assigned to an unsupported object (like FHRP Group)."""
        ip = make_nb_obj("ADDRESSES_IPV4", assigned_object_type="ipam.fhrpgroup")
        with pytest.raises(TypeError, match="Unsupported assigned_object_type"):
            scd._resolve_ip_to_host(ip)


class TestHostnameResolution:
    @pytest.mark.parametrize("filter_return", [[], None])
    def test_get_by_hostname_no_ips(self, scd, filter_return):
        scd.netbox.ipam.ip_addresses.filter.return_value = filter_return
        with pytest.raises(NetboxNotFoundError, match="not found in NetBox"):
            scd.get_device_by_hostname("missing.host.com")

    def test_get_by_hostname_all_unmatched(self, scd, mocker, make_nb_obj):
        """IPs found, but none of them are attached to a Host (skipped)."""
        scd.netbox.ipam.ip_addresses.filter.return_value = [
            make_nb_obj("ADDRESSES_IPV4", address="1.1.1.1")
        ]
        mocker.patch.object(
            scd, "_resolve_ip_to_host", side_effect=NetboxUnmatchedIPError("test")
        )

        with pytest.raises(
            NetboxNotFoundError,
            match="resolves to IPs, but they don't resolve to any hosts",
        ):
            scd.get_device_by_hostname("unattached.host.com")

    def test_get_by_hostname_ambiguous(self, scd, mocker, make_nb_obj):
        """IPs found resolving to DIFFERENT hosts."""
        ip1 = make_nb_obj("ADDRESSES_IPV4", address="1.1.1.1")
        ip2 = make_nb_obj("ADDRESSES_IPV4", address="2.2.2.2")
        scd.netbox.ipam.ip_addresses.filter.return_value = [ip1, ip2]

        dev1 = make_nb_obj("DEVICE_PHYSICAL", id=1)
        dev2 = make_nb_obj("DEVICE_PHYSICAL", id=2)
        mocker.patch.object(scd, "_resolve_ip_to_host", side_effect=[dev1, dev2])

        with pytest.raises(NetboxMultipleResultsError, match="Ambiguous hostname"):
            scd.get_device_by_hostname("ambiguous.host.com")

    @pytest.mark.parametrize("multiple_same_host", [True, False])
    def test_get_by_hostname_success(
        self, scd, mocker, make_nb_obj, multiple_same_host
    ):
        """If multiple IPs are found, but they all map back to the SAME host, should be fine."""
        ips = [make_nb_obj("ADDRESSES_IPV4", address="1.1.1.1")]
        if multiple_same_host:
            ips.append(make_nb_obj("ADDRESSES_IPV4", address="1.1.1.1"))
        scd.netbox.ipam.ip_addresses.filter.return_value = ips

        dev = make_nb_obj("DEVICE_PHYSICAL", id=99)
        mocker.patch.object(
            scd, "_resolve_ip_to_host", return_value=dev
        )  # Same dev returned both times

        assert scd.get_device_by_hostname("valid.host.com") == dev


# ==========================================
# Component Lookups (Racks, Ifaces, Disks)
# ==========================================


class TestComponentLookups:
    def test_get_rack_from_device(self, scd, make_nb_obj):
        dev = make_nb_obj("DEVICE_PHYSICAL")
        rack = MagicMock(spec=Racks, facility_id="FAC123")
        dev.rack = rack
        assert scd.get_rack_from_device(dev) == rack

    def test_get_rack_wrong_type(self, scd, make_nb_obj):
        vm = make_nb_obj("DEVICE_VIRTUAL")
        with pytest.raises(TypeError, match="not a physical device"):
            scd.get_rack_from_device(vm)

    def test_get_rack_none(self, scd, make_nb_obj):
        dev = make_nb_obj("DEVICE_PHYSICAL", rack=None)
        with pytest.raises(NetboxInvalidError, match="not in a rack"):
            scd.get_rack_from_device(dev)

    def test_get_rack_no_facility(self, scd, make_nb_obj):
        dev = make_nb_obj("DEVICE_PHYSICAL")
        dev.rack = MagicMock(spec=Racks, facility_id=None)
        with pytest.raises(NetboxInvalidError, match="has no facility id"):
            scd.get_rack_from_device(dev)

    @pytest.mark.parametrize(
        "interface_types, expected_count",
        [
            (["mac", "lag", "invalid"], 2),  # Multiple ints, one dropped
            (["mac", "mac"], 2),  # Multiple valid MACs
            (["lag"], 1),  # Single LAG, no MAC
            (["invalid"], 0),  # Only dropped
        ],
    )
    def test_get_interfaces_filtering(
        self, scd, make_nb_obj, interface_types, expected_count
    ):
        dev = make_nb_obj("DEVICE_PHYSICAL", name="router01")

        def mock_factory(kind):
            if kind == "mac":
                return make_nb_obj("INTERFACES_PHYSICAL")
            if kind == "lag":
                return make_nb_obj("INTERFACES_PHYSICAL_LAGS")
            return make_nb_obj("INTERFACES_PHYSICAL", mac_address=None)

        mock_list = [mock_factory(t) for t in interface_types]
        scd.netbox.dcim.interfaces.filter.return_value = mock_list

        if expected_count == 0:
            with pytest.raises(
                NetboxNotFoundError,
                match="No valid \\(has MAC, or in a LAG\\) interfaces found",
            ):
                scd.get_interfaces_from_device(dev)
            return

        interfaces = scd.get_interfaces_from_device(dev)

        assert len(interfaces) == expected_count

        # Ensure no "invalid" types ever make it through
        for iface in interfaces:
            if iface.mac_address is None:
                assert iface.type.value == "lag"

    @pytest.mark.parametrize("host_fixture", ["DEVICE_PHYSICAL", "DEVICE_VIRTUAL"])
    def test_get_interfaces_none_found(self, scd, make_nb_obj, host_fixture):
        host = make_nb_obj(host_fixture, name="host01")
        scd.netbox.dcim.interfaces.filter.return_value = []
        scd.netbox.virtualization.interfaces.filter.return_value = []

        with pytest.raises(NetboxNotFoundError):
            scd.get_interfaces_from_device(host)

    def test_get_interfaces_wrong_type(self, scd):
        with pytest.raises(TypeError, match="Unsupported device type"):
            scd.get_interfaces_from_device(MagicMock(spec=IpAddresses))

    @pytest.mark.parametrize(
        "has_device, has_vm",
        [
            (True, False),  # Device interface
            (False, True),  # VM interface
        ],
    )
    def test_get_addresses_from_interface(self, scd, make_nb_obj, has_device, has_vm):
        iface = make_nb_obj("INTERFACES_PHYSICAL", count_ipaddresses=2)

        if not has_device:
            del iface.device

        if has_vm:
            iface = make_nb_obj("INTERFACES_VIRTUAL", count_ipaddresses=2)

        # Mock mixed IPv4 and IPv6 return
        ip_v4 = make_nb_obj("ADDRESSES_IPV4", family=MagicMock(value=4))
        ip_v6 = make_nb_obj("ADDRESSES_IPV4", family=MagicMock(value=6))

        scd.netbox.ipam.ip_addresses.filter.return_value = [ip_v4, ip_v6]

        addrs = scd.get_addresses_from_interface(iface)
        assert len(addrs) == 1
        assert addrs[0] == ip_v4

    def test_get_addresses_unsupported_interface(self, scd, make_nb_obj):
        """Tests that passing an interface not belonging to a physical device or VM raises an error."""
        iface = make_nb_obj("INTERFACES_PHYSICAL", count_ipaddresses=2)
        del (
            iface.device
        )  # Strip standard device attachment to force fallback to `else` block

        with pytest.raises(NetboxInvalidError, match="Unsupported interface type"):
            scd.get_addresses_from_interface(iface)

    def test_get_addresses_no_ipv4(self, scd, make_nb_obj):
        """Tests raising an error if an interface has IPs, but none are IPv4."""
        iface = make_nb_obj("INTERFACES_PHYSICAL", count_ipaddresses=1)
        ip_v6 = make_nb_obj("ADDRESSES_IPV4", family=MagicMock(value=6))

        scd.netbox.ipam.ip_addresses.filter.return_value = [ip_v6]

        with pytest.raises(NetboxNotFoundError, match="had no IPv4 addresses"):
            scd.get_addresses_from_interface(iface)

    def test_get_addresses_zero_count(self, scd):
        iface = MagicMock(spec=Interfaces, count_ipaddresses=0)
        with pytest.raises(NetboxNotFoundError):
            scd.get_addresses_from_interface(iface)

    def test_get_disks_from_device(self, scd, make_nb_obj):
        vm = make_nb_obj("DEVICE_VIRTUAL", id=5)
        scd.netbox.virtualization.virtual_disks.filter.return_value = ["disk1", "disk2"]

        assert scd.get_disks_from_device(vm) == ["disk1", "disk2"]

    def test_get_disks_none_found(self, scd, make_nb_obj):
        vm = make_nb_obj("DEVICE_VIRTUAL", id=5)
        scd.netbox.virtualization.virtual_disks.filter.return_value = []

        with pytest.raises(NetboxNotFoundError, match="No virtual disks were found"):
            scd.get_disks_from_device(vm)

    def test_get_disks_wrong_type(self, scd, make_nb_obj):
        dev = make_nb_obj("DEVICE_PHYSICAL")
        with pytest.raises(TypeError, match="Unsupported device type"):
            scd.get_disks_from_device(dev)
