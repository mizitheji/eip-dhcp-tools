import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eipdhcp.api import Api                    # noqa: E402
from eipdhcp.config import Settings            # noqa: E402
from fake_server import FakeSDS                # noqa: E402

HEADER = "space,site,network,dhcp_server,start_address,end_address,gateway,dns_servers,failover_channel\n"


@pytest.fixture
def fake():
    return FakeSDS()


@pytest.fixture
def api(fake):
    return Api(fake, sleep=lambda s: None)


@pytest.fixture
def settings(tmp_path):
    return Settings(host="test-host", user="u", password="p", logs_dir=str(tmp_path / "logs"), runs_dir=str(tmp_path / "runs"))


@pytest.fixture
def csv_file(tmp_path):
    def make(*rows):
        p = tmp_path / "scopes.csv"
        p.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
        return str(p)
    return make


# two explicit rows (everything comes from the CSV)
WM = 'TestSpace,Site A,10.1.18.0/23,smart.dhcp.example,10.1.18.10,10.1.19.254,10.1.18.4,"10.0.0.2,10.0.0.3",fo1.dhcp'
WIFI = 'TestSpace,Site WiFi,10.2.4.0/24,smart.dhcp.example,10.2.4.51,10.2.4.244,10.2.4.3,"10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5",fo1.dhcp'
