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

import copy
import datetime
import logging
import os
import re
import socket
import typing
import io

from foris_controller.app import app_info
from foris_controller.utils import RWLock

logger = logging.getLogger(__name__)


class Slices:
    DEFAULT_SIZE = 50

    def __init__(self, size: int = DEFAULT_SIZE):
        self.lock: RWLock = RWLock(app_info["lock_backend"])
        self._list: typing.List[dict] = []
        self.size = size

    def list(self) -> typing.List[dict]:
        with self.lock.readlock:
            return copy.deepcopy(self._list)

    def append(self, records: typing.List[dict]) -> dict:
        timestamp = datetime.datetime.utcnow().timestamp()
        record = {"timestamp": timestamp, "records": records}
        with self.lock.writelock:
            self._list.append(record)
        return record


class Collectd:
    EXPORTABLES = [(r"[^/]*/(cpu-\d+)/percent-active", ["value"])]

    def __init__(self, slices: Slices):
        self.latest: typing.List[typing.Tuple[str, str]] = []
        socket_path = os.environ.get("FC_COLLECTD_PATH", "/var/run/follectd.sock")
        self.socket_path = socket_path
        self.slices = slices
        self.lock: app_info["lock_backend"].Lock = app_info["lock_backend"].Lock()

    def _write_read_socket(self, sock: socket.socket, input_data: str) -> str:
        sock.sendall(input_data)
        buf = ""
        count: typing.Optional[int] = None

        while True:
            # TODO add some timeout for reading
            received = sock.recv(1024)
            buf += received.decode("utf-8")
            if not count:
                # X Values in output determines when the socket reading ends
                match = re.match(r"(\d+) Values{0,1} found", buf)
                if match:
                    count = int(match.group(1))

            if count is not None and buf.count("\n") >= count + 1:
                break

        logger.debug("Data read from the socket (%d)\n%s", len(buf), buf)
        return buf

    def _listval(self, sock: socket.socket) -> typing.Dict[str, float]:
        logger.debug("Calling LISTVAL")
        resp: str = self._write_read_socket(sock, b"LISTVAL\n")
        return {
            e[1]: float(e[0])
            for e in [line.split(" ") for line in resp.strip().splitlines(False)]
            if len(e) == 2
        }

    def _getval(
        self, sock: socket.socket, key: str, values: typing.List[str]
    ) -> typing.List[typing.Dict[str, float]]:
        logger.debug("Calling GETVAL %s", key)
        resp: str = self._write_read_socket(sock, f"GETVAL {key}\n".encode("utf8"))

        res: typing.List[typing.Tuple[str, float]] = []
        for line in resp.strip().splitlines(False):
            splitted = line.split("=")
            if len(splitted) == 2 and " " not in line:
                name, value = splitted
                if name in values:
                    res.append({"key": name, "value": float(value)})
                value = float(value)

        return res

    def get_latest(self) -> typing.Optional[dict]:
        logger.debug("Getting latest data")
        with self.lock:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                logger.debug("Connected to socket '%s'", self.socket_path)

                listval = self._listval(sock)
                logger.debug("Listed items '%s'", listval)

                to_query: typing.List[str]
                if listval == self.latest:
                    return None  # no change since last time
                else:
                    if self.latest is None:
                        to_query = list(listval)
                    else:
                        # find changes
                        to_query = [
                            e
                            for e in listval
                            if e not in self.latest or listval[e] != self.latest[e]
                        ]

                self.latest = listval

                # query only exportables
                queries: typing.Tuple["string", "string", typing.List[str]] = []
                for key in to_query:
                    for regex, values in Collectd.EXPORTABLES:
                        match = re.match(regex, key)
                        if match:
                            queries.append((match.group(1), key, values))

                # perform actual queries
                res_records: list[dict] = []
                for name, query, values in queries:
                    res_records.append({"name": name, "values": self._getval(sock, query, values)})

                # don't propagate empty
                if not res_records:
                    return None

                return self.slices.append(res_records)

            except socket.error as e:
                logger.warn("Socket error occured '%s'", e)
                return None  # we don't want to terminate on error, just return no data
            finally:
                # try to close the socket
                try:
                    socket.close()
                except Exception:
                    pass


collectd = Collectd(Slices())
