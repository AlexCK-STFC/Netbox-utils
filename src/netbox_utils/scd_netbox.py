"""Library of common functionality used for interacting with SCD's NetBox instance."""

from __future__ import annotations

import configparser
import logging
import os.path
import warnings
from os import PathLike
from typing import Any, Iterable, Mapping, Optional, Sequence, Union, cast

import pynetbox
import requests
from pynetbox.core.response import Record
from pynetbox.models.dcim import Devices, Interfaces, Racks
from pynetbox.models.ipam import IpAddresses
from pynetbox.models.virtualization import VirtualMachines


class NetboxError(Exception):
    """Base class for all NetBox lookup and validation errors."""


class NetboxNotFoundError(NetboxError):
    """Raised when a requested object cannot be found in NetBox."""


class NetboxInvalidError(NetboxError):
    """Raised when an object is ambiguous, invalid, or logically inconsistent."""


class NetboxMultipleResultsError(NetboxInvalidError):
    """Raised when a lookup unexpectedly returns multiple results."""


class NetboxUnmatchedIPError(NetboxInvalidError):
    """Raised when an IP address does not correspond to any host."""


_host_type = {
    Devices: "Device",
    VirtualMachines: "VM",
}

Host = Union[Devices, VirtualMachines]


class SCDNetbox:
    """Client for interacting with SCD's NetBox instance.

    This class may be used directly, or subclassed by other tools that need
    convenience helpers or extended NetBox operations.
    """

    def __init__(
        self,
        config: Union[
            str,
            bytes,
            PathLike,
            Sequence[Union[str, bytes, PathLike]],
            Mapping[str, Mapping[str, Any]],
        ] = "aquilon",
        additional_config_name: Optional[str] = None,
        additonal_config_name: Optional[str] = None,  # Sic: Backwards-compatibility
    ):
        """Initialise the SCDNetbox instance and configure a NetBox API session.

        Args:
            config: Configuration input. May be:
                * "aquilon": loads `/var/quattor/etc/scd_netbox.cfg` and `~/.scd_netbox.cfg`
                * A path to a config file
                * A list/tuple of config file paths
                * A parsed configuration mapping (dict)
                Defaults to "aquilon".
            additional_config_name: Optional additional config name (without `.cfg`)
                to load in addition to the defaults on the Aquilon broker machine when `config="aquilon"`.
                Defaults to None.
            additonal_config_name: Deprecated spelling kept for compatibility. Defaults to None.

        Raises:
            ValueError: If configuration input is invalid or missing required keys.
        """
        if additonal_config_name is not None:
            warnings.warn(
                "'additonal_config_name' is deprecated, use 'additional_config_name' instead",
                DeprecationWarning,
                stacklevel=2,
            )
            if additional_config_name is not None:
                raise ValueError(
                    "Pass only 'additional_config_name'; 'additonal_config_name' is deprecated."
                )
            additional_config_name = additonal_config_name

        self.config = configparser.ConfigParser()

        self._load_config(config, additional_config_name)

        self._validate_config()

        netbox_session = requests.Session()

        if "cert_path" in self.config["netbox"]:
            if self.config["netbox"]["cert_path"].lower() == "false":
                netbox_session.verify = False
            else:
                netbox_session.verify = self.config["netbox"]["cert_path"]

        self.netbox = pynetbox.api(
            self.config["netbox"]["url"], token=self.config["netbox"]["token"]
        )
        self.netbox.http_session = netbox_session

    def _aquilon_validate_config_paths(self, name: str, err: str) -> list[str]:
        """Return expected Aquilon config paths and validate that at least one exists.

        Args:
            name: Base name of the config file (without extension).
            err: Error message raised if neither file exists.

        Returns:
            A list of expected paths.

        Raises:
            ValueError: If the expected files are not present.
        """
        paths = [
            f"/var/quattor/etc/{name}.cfg",
            os.path.expanduser(f"~/.{name}.cfg"),
        ]
        if not any(os.path.isfile(path) for path in paths):
            raise ValueError(err)
        return paths

    def _load_config(
        self,
        config: Union[
            str,
            bytes,
            PathLike,
            Sequence[Union[str, bytes, PathLike]],
            Mapping[str, Mapping[str, Any]],
        ],
        additional_name: Optional[str],
    ):
        """Load configuration from files or mappings.

        Args:
            config: Configuration source (string/filename, iterable of filenames,
                or configuration mapping).
            additional_name: Optional name of an additional Aquilon-style file to load.

        Raises:
            ValueError: If no valid configuration source is found.
        """
        # Case 1: default aquilon config
        if config == "aquilon":
            self.config.read(
                self._aquilon_validate_config_paths(
                    name="scd_netbox",
                    err="""Using 'aquilon' config option, but scd_netbox.cfg not found in expected paths."
                Are you running this script on the Aquilon broker,
                or should you use a different config option?""",
                )
            )

            if additional_name:
                self.config.read(
                    self._aquilon_validate_config_paths(
                        name=additional_name,
                        err=f"""'additional_name' config option used, but {additional_name}.cfg
                        not found in /var/quattor/etc or as a dotfile in ~/""",
                    )
                )
            return

        # Case 2: mapping/dict provided
        if isinstance(config, Mapping):
            self.config.read_dict(config)
            return

        # Case 3: single file path
        if isinstance(config, (str, bytes, PathLike)) and os.path.isfile(config):
            self.config.read(config)
            return

        # Case 4: iterable of valid paths
        if isinstance(config, Iterable) and all(
            isinstance(x, (str, bytes, PathLike)) and os.path.exists(x) for x in config
        ):
            self.config.read(config)
            return

        # No valid option matched
        raise ValueError(
            'config must be one of "aquilon", a path, an iterable of paths, or a dict'
        )

    def _validate_config(self):
        """Validate that required NetBox configuration fields are present.

        Raises:
            ValueError: If required configuration sections or fields are missing.
        """
        required = {"netbox": ["url", "token"]}

        for section, keys in required.items():
            if section not in self.config:
                raise ValueError(f"Missing required config section: [{section}]")

            missing = [k for k in keys if k not in self.config[section]]
            if missing:
                raise ValueError(
                    f"Missing required keys in section [{section}]: {missing}"
                )

    @classmethod
    def from_connection(
        cls, url: str, token: str, cert_path: Optional[str] = None, **extra_sections
    ):
        """Create an SCDNetbox instance directly from connection parameters.

        Args:
            url: NetBox URL.
            token: NetBox API token.
            cert_path: Optional certificate path. If set to ``"false"``, SSL
                verification is disabled. Defaults to None.
            **extra_sections: Additional config sections to include.

        Returns:
            A new SCDNetbox instance configured with the provided settings.
        """
        cfg = {
            "netbox": {
                "url": url,
                "token": token,
            },
            **extra_sections,
        }
        if cert_path is not None:
            cfg["netbox"]["cert_path"] = cert_path
        return cls(config=cfg)

    def _get_host(self, device_filter: dict, vm_filter: dict, desc: str) -> Host:
        """Resolve a host using device and VM filters.

        Args:
            device_filter: Filter parameters for a device lookup.
            vm_filter: Filter parameters for a VM lookup.
            desc: Text description used in error messages.

        Returns:
            A matching device or virtual machine.

        Raises:
            NetboxMultipleResultsError: If either filter matches multiple objects.
            NetboxInvalidError: If both a device and VM match.
            NetboxNotFoundError: If nothing matches.
        """
        # Query device
        try:
            device = self.netbox.dcim.devices.get(**device_filter)
        except ValueError as exc:
            raise NetboxMultipleResultsError(
                f"Multiple devices found with {desc}"
            ) from exc

        # Query VM
        try:
            vm = self.netbox.virtualization.virtual_machines.get(**vm_filter)
        except ValueError as exc:
            raise NetboxMultipleResultsError(f"Multiple VMs found with {desc}") from exc

        # Ambiguity: both exist
        if device and vm:
            raise NetboxInvalidError(
                f"{desc} matches both a Device ({device}) and a VM ({vm})"
            )

        if device:
            logging.debug("Got device %s for %s", device, desc)
            return device

        if vm:
            logging.debug("Got VM %s for %s", vm, desc)
            return vm

        raise NetboxNotFoundError(f"No host found for {desc}")

    def get_device_by_magdb_id(self, magdb_id: Union[str, int]) -> Host:
        """Resolve a host by its MagDB system ID.

        Args:
            magdb_id: MagDB system identifier.

        Returns:
            The matching device or VM.

        Raises:
            NetboxNotFoundError: If no matching host exists.
        """
        desc = f"MagDB ID {magdb_id}"
        return self._get_host(
            device_filter={"cf_magdb_system_id": magdb_id},
            vm_filter={"cf_magdb_system_id": magdb_id},
            desc=desc,
        )

    def get_device_by_name(self, name: str) -> Host:
        """Resolve a host by its NetBox object name.

        Args:
            name: The name of the device or VM (usually matches DNS hostname).

        Returns:
            The matching device or VM.

        Raises:
            NetboxNotFoundError: If no matching host exists.
        """
        desc = f"name '{name}'"
        return self._get_host(
            device_filter={"name": name},
            vm_filter={"name": name},
            desc=desc,
        )

    def _resolve_ip_to_host(self, ip_address: Union[IpAddresses, str]) -> Host:
        """Resolve an IP address to its assigned host.

        Args:
            ip_address: A NetBox IPAddress object or a string representing an IP
                (with or without prefix length).

        Returns:
            The device or VM associated with the IP.

        Raises:
            NetboxNotFoundError: If a string IP does not exist in NetBox.
            NetboxUnmatchedIPError: If the IP is not assigned to any host.
            TypeError: If the IP is assigned to an unsupported NetBox object type.
        """
        if isinstance(ip_address, str):
            ip_str = ip_address
            ip_address = cast(
                IpAddresses, self.netbox.ipam.ip_addresses.get(address=ip_str)
            )  # From testing, appears to work with/without prefix length.
            # Not sure about duplicates, hopefully they just don't/can't exist?
            if ip_address is None:
                raise NetboxNotFoundError("IP {ip_str} not found in Netbox")

        aot = ip_address.assigned_object_type

        if aot is None:
            raise NetboxUnmatchedIPError(f"IP {ip_address} isn't assigned to a host")

        if aot == "dcim.interface":
            iface = cast(Interfaces, ip_address.assigned_object)
            device = cast(Devices, iface.device)
            return device

        if aot == "virtualization.vminterface":
            iface = cast(Interfaces, ip_address.assigned_object)
            vm = cast(VirtualMachines, iface.virtual_machine)
            return vm

        raise TypeError(f"Unsupported assigned_object_type {aot}, expected Device/VM")

    def get_device_by_hostname(self, hostname: str) -> Host:
        """Resolve a DNS hostname to a host via its associated IP address records.

        Args:
            hostname: DNS hostname (may differ from NetBox object name, usually the same).

        Returns:
            The device or VM associated with the hostname.

        Raises:
            NetboxNotFoundError: If no relevant DNS records exist.
            NetboxNotFoundError: If resolved IPs do not map to any hosts.
            NetboxMultipleResultsError: If the hostname maps to multiple distinct hosts.
        """
        dns_name = hostname.strip().lower()

        ip_qs = self.netbox.ipam.ip_addresses.filter(dns_name=dns_name)
        if ip_qs is None:
            raise NetboxNotFoundError(
                f"DNS hostname {hostname} not found in NetBox, or resolves to no IPs."
                "Try getting host by netbox name instead?"
            )

        ip_addresses = list(ip_qs)
        if not ip_addresses:
            raise NetboxNotFoundError(
                f"DNS hostname {hostname} not found in NetBox, or resolves to no IPs."
                "Try getting host by netbox name instead?"
            )

        # Resolve each IP to a host
        resolved = set()  # identifies unique hosts
        details = []  # for error diagnostics

        for ip in ip_addresses:
            try:
                host = self._resolve_ip_to_host(ip)
                resolved.add(host)
                details.append(f"{ip.address} -> {_host_type[type(host)]}: {host.id}")
            except NetboxUnmatchedIPError as e:
                logging.debug("Hostname %s: %s, skipped", hostname, e)
                continue

        # No hosts
        if len(resolved) == 0:
            raise NetboxNotFoundError(
                f"Hostname {hostname} resolves to IPs, but they don't resolve to any hosts."
            )

        # Ambiguous: different hosts share the same DNS name
        if len(resolved) > 1:
            raise NetboxMultipleResultsError(
                f"Ambiguous hostname '{hostname}': resolves to multiple hosts: {'; '.join(details)}"
            )

        # Multiple IPs but same host — fine
        if len(ip_addresses) > 1:
            logging.debug(
                "Hostname %s has multiple IPs but all map to the same host (%s)",
                hostname,
                "; ".join(details),
            )

        host = resolved.pop()
        logging.debug("Got host %s for hostname %s", host, hostname)
        return host

    def get_rack_from_device(self, device: Devices) -> Racks:
        """Return the rack containing a physical device.

        Ensures that the device is racked and that the rack has a Facility ID.

        Args:
            device: A physical NetBox device.

        Returns:
            The rack containing the device.

        Raises:
            TypeError: If the input is not a physical Device object.
            NetboxInvalidError: If the device is not in a rack.
            NetboxInvalidError: If the rack lacks a Facility ID.
        """
        if not isinstance(device, Devices):
            raise TypeError(f"Host {device} is not a physical device.")

        rack = cast(Racks, device.rack)

        if rack is None:
            raise NetboxInvalidError(f"Device {device} is not in a rack.")

        if rack.facility_id is None:
            raise NetboxInvalidError(
                f"Rack {rack} of device {device} has no facility id. Is it a proper rack?"
            )

        return rack

    def get_interfaces_from_device(self, device: Host) -> list[Interfaces]:
        """Retrieve interfaces for a device or VM.

        Only interfaces with a MAC address, or that belong to a LAG, are returned.

        Args:
            device: A device or virtual machine.

        Returns:
            List of valid interfaces.

        Raises:
            TypeError: If the object is neither a Device nor a VM.
            NetboxNotFoundError: If no valid interfaces exist for the host.
        """
        if isinstance(device, Devices):
            filter_interfaces = self.netbox.dcim.interfaces.filter(device=device.name)
        elif isinstance(device, VirtualMachines):
            filter_interfaces = self.netbox.virtualization.interfaces.filter(
                virtual_machine=device.name
            )
        else:
            raise TypeError(
                f"Unsupported device type for getting interfaces {type(device)}"
            )

        if len(filter_interfaces) == 0:
            raise NetboxNotFoundError(
                f"No interfaces found for {_host_type[type(device)]} {device}"
            )

        interfaces = []
        unusedintf = 0
        for interface in filter_interfaces:
            if interface.mac_address:
                interfaces.append(interface)
            elif (
                hasattr(interface, "type") and interface.type.value == "lag"
            ):  # the hasattr check is prob unneccesary, type is required. But doesn't hurt.
                interfaces.append(interface)
            else:
                unusedintf += 1

        if unusedintf:
            logging.warning(
                "%s non-lag interfaces without MAC address were not included",
                unusedintf,
            )

        if len(interfaces) == 0:
            raise NetboxNotFoundError(
                f"No valid (has MAC, or in a LAG) interfaces found for {_host_type[type(device)]} {device}"
                f"{unusedintf} non-lag interfaces without a MAC address were not included."
            )

        return interfaces

    def get_addresses_from_interface(self, interface: Interfaces) -> list[IpAddresses]:
        """Return IPv4 addresses assigned to an interface.

        Args:
            interface: A device or VM interface.

        Returns:
            List of IPv4 address objects associated with the interface.

        Raises:
            NetboxNotFoundError: If the interface has no assigned addresses.
            NetboxInvalidError: If the interface isn't bound to a physical device or a VM.
            NetboxNotFoundError: If the interface has addresses, but they're not supported (i.e. IPv6).
        """
        if interface.count_ipaddresses == 0:
            raise NetboxNotFoundError(f"Interface {interface} has no associated IPs")

        if hasattr(interface, "device"):
            all_addresses = self.netbox.ipam.ip_addresses.filter(
                interface_id=interface.id
            )
        elif hasattr(interface, "virtual_machine"):
            all_addresses = self.netbox.ipam.ip_addresses.filter(
                vminterface_id=interface.id
            )
        else:
            raise NetboxInvalidError(
                f"Unsupported interface type for interface {interface}"
                "Must belong to a physical device, or a VM"
            )

        ipv4_addresses = []
        for address in all_addresses:
            # We currently only support IPv4 addresses via broker assignment
            if address.family.value == 4:
                ipv4_addresses.append(address)
            else:
                logging.warning(
                    "Interface %s has an address (%s) in NetBox with an unsupported family (%s),"
                    " which was ignored",
                    interface.name,
                    address.address,
                    address.family.label,
                )

        if len(ipv4_addresses) == 0:
            raise NetboxNotFoundError(
                f"Interface {interface} had no IPv4 addresses."
                "It's likely it had some with unsupported families, check warning logs"
            )

        return ipv4_addresses

    def get_disks_from_device(
        self, device: VirtualMachines
    ) -> list[Record]:  # No more specific type in model, ATOW
        """Return virtual disks attached to a virtual machine.

        Args:
            device: A virtual machine.

        Returns:
            List of virtual disk objects.

        Raises:
            TypeError: If the object is not a virtual machine.
            NetboxNotFoundError: No virtual disks found.
        """
        if isinstance(device, VirtualMachines):
            filtered_disks = self.netbox.virtualization.virtual_disks.filter(
                virtual_machine_id=device.id
            )
        else:
            raise TypeError(f"Unsupported device type for virtual disks {type(device)}")

        filtered_disks = list(filtered_disks)
        if len(filtered_disks) == 0:
            raise NetboxNotFoundError(f"No virtual disks were found for VM {device}")

        return list(filtered_disks)
