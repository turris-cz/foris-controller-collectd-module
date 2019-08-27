#
# foris-controller-collectd-module
# Copyright (C) 2019 CZ.NIC, z.s.p.o. (http://www.nic.cz/)
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
#

import functools
import pathlib
import pytest
import shutil
import subprocess
import time

from foris_controller_testtools.fixtures import (
    only_message_buses,
    only_backends,
    backend,
    infrastructure,
    start_buses,
    mosquitto_test,
    ubusd_test,
    notify_api,
    file_root_init,
    FILE_ROOT_PATH,
)


@pytest.fixture(scope="session")
def follectd():
    conf_path = pathlib.Path("/tmp/follectd.conf")
    with conf_path.open("w") as f:
        f.write(
            """\
BaseDir     "/tmp/follectd"
PIDFile     "/tmp/follectd.pid"
Interval    5
ReadThreads 1
WriteThreads 1

LoadPlugin cpu
LoadPlugin df
LoadPlugin exec
LoadPlugin interface
LoadPlugin memory
LoadPlugin network
LoadPlugin unixsock

<Plugin cpu>
    ReportByState false
    ReportByCpu true
</Plugin>

<Plugin df>
    ValuesAbsolute true
    ValuesPercentage  false
    ReportByDevice true
</Plugin>

<Plugin interface>
    Interface "lo"
    Interface "/^ifb.*/"
    Interface "/^gre.*/"
    Interface "/^teql.*/"
    IgnoreSelected true
</Plugin>

<Plugin memory>
    ValuesAbsolute true
    ValuesPercentage false
</Plugin>

<Plugin unixsock>
  SocketFile "/tmp/follectd.sock"
  SocketGroup "nogroup"
  SocketPerms "0777"
  DeleteSocket true
</Plugin>

<Plugin exec>
    Exec "nobody" "/tmp/foris_files/usr/libexec/follectd/neighbours.sh"
</Plugin>
"""
        )
        f.flush()

    socket_path = pathlib.Path("/tmp/follectd.sock")
    try:
        socket_path.unlink()
    except Exception:
        pass

    collectd_path = pathlib.Path("/usr/sbin/collectd")
    follectd_instance = subprocess.Popen(
        [
            str(collectd_path) if collectd_path.exists() else "collectd",
            "-f",
            "-C",
            "/tmp/follectd.conf",
        ]
    )

    while not socket_path.exists():
        time.sleep(0.2)

    yield follectd_instance

    follectd_instance.kill()

    try:
        socket_path.unlink()
    except Exception:
        pass

    try:
        conf_path.unlink()
    except Exception:
        pass


def test_list(infrastructure, start_buses, follectd):
    res = infrastructure.process_message(
        {"module": "collectd", "action": "list", "kind": "request"}
    )
    assert "slices" in res["data"]


@pytest.mark.only_message_buses(["mqtt"])
@pytest.mark.only_backends(["openwrt"])
def test_new_slice_announcements(mosquitto_test, start_buses, infrastructure, follectd):
    filters = [("collectd", "new_slice")]

    notifications = infrastructure.get_notifications([], filters=filters)
    assert "timestamp" in notifications[-1]["data"]
    assert "records" in notifications[-1]["data"]
    timestamp1 = notifications[-1]["data"]["timestamp"]

    notifications = infrastructure.get_notifications(notifications, filters=filters)
    assert "timestamp" in notifications[-1]["data"]
    assert "records" in notifications[-1]["data"]
    timestamp2 = notifications[-1]["data"]["timestamp"]

    assert timestamp1 < timestamp2
    assert round(timestamp2 - timestamp1, 0) >= 2.0  # there is 2s delay in announcer

    records = functools.reduce(lambda x, y: x + y["data"]["records"], notifications, [])
    for name, required_values in [("cpu", {"value"})]:
        filtered_records = [e for e in records if e["name"].startswith(name)]
        assert len(filtered_records) > 0
        for record in filtered_records:
            assert set([e["key"] for e in record["values"]]) == required_values
