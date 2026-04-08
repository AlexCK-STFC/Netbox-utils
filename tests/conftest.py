import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

# pynetbox imports
from pynetbox.core.response import Record
from pynetbox.models.dcim import Devices, Interfaces
from pynetbox.models.ipam import IpAddresses, Prefixes
from pynetbox.models.virtualization import VirtualMachines


@pytest.fixture(scope="session")
def netbox_testdata():
    """
    Loads fake objects from JSON files and provides them as a fixture.
    The 'session' scope means this runs once for the whole test run.
    """
    result = SimpleNamespace()

    DATA_DIR = Path(__file__).parent / "testdata"

    result.API = SimpleNamespace(
        base_url="http://netbox.example.org/api/",
        token="a8352bb6-75f5-4b6e-ad0e-3f21b8f0615b",
        session_key="42",
    )
    result.ENDPOINT = deepcopy(result.API)

    mapping = {
        "device_physical": Devices,
        "device_virtual": VirtualMachines,
        "disks_virtual": Record,
        "interfaces_physical": Interfaces,
        "interfaces_physical_lags": Interfaces,
        "interfaces_virtual": Record,
        "addresses_ipv4": IpAddresses,
        "addresses_ipv6": IpAddresses,
        "prefixes_ipv4": Prefixes,
    }

    for name, model in mapping.items():
        file_path = DATA_DIR / f"{name}.json"

        with open(file_path, encoding="utf-8") as json_file:
            data = json.load(json_file)

            if isinstance(data, dict):
                obj = model(
                    values=data,
                    api=deepcopy(result.API),
                    endpoint=deepcopy(result.ENDPOINT),
                )
            else:  # Assume list
                obj = [
                    model(
                        values=v,
                        api=deepcopy(result.API),
                        endpoint=deepcopy(result.ENDPOINT),
                    )
                    for v in data
                ]
            setattr(result, name.upper(), obj)

    return result


@pytest.fixture
def make_nb_obj(mocker, netbox_testdata):
    """
    Factory that clones a base test data object and applies overrides.
    """

    def _factory(model_type, index=0, **kwargs):
        # Fetch the base object from nbobjects (e.g., 'DEVICE_PHYSICAL')
        base_obj = getattr(netbox_testdata, model_type.upper(), None)

        # Determine if it's a list or single object and deepcopy
        if isinstance(base_obj, list):
            if index is Ellipsis:
                return [deepcopy(o) for o in base_obj]
            obj = deepcopy(base_obj[index]) if base_obj else mocker.MagicMock()
        else:
            obj = deepcopy(base_obj) if base_obj else mocker.MagicMock()

        # Apply property overrides
        for k, v in kwargs.items():
            setattr(obj, k, v)
        return obj

    return _factory
