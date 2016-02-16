#
# Copyright (C) 2014-2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Ben Dyer <ben_dyer@mac.com>
#         Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import division, absolute_import, print_function, unicode_literals
import os
import sys
import fcntl
import socket
import struct
import select
from logging import getLogger
from .common import DriverError, RxFrame

logger = getLogger(__name__)

SIOCGSTAMP = 0x8906

# Python 3.3+'s socket module has support for SocketCAN when running on Linux. Use that if possible.
# noinspection PyBroadException
try:
    # noinspection PyStatementEffect
    socket.CAN_RAW

    def get_socket(ifname):
        s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((ifname, ))
        return s

except Exception:
    import ctypes  # @UnusedImport
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    # from linux/can.h
    CAN_RAW = 1

    # from linux/socket.h
    AF_CAN = 29

    from socket import SOL_SOCKET

    SOL_CAN_BASE = 100
    SOL_CAN_RAW = SOL_CAN_BASE + CAN_RAW
    CAN_RAW_FILTER = 1                      # set 0 .. n can_filter(s)
    CAN_RAW_ERR_FILTER = 2                  # set filter for error frames
    CAN_RAW_LOOPBACK = 3                    # local loopback (default:on)
    CAN_RAW_RECV_OWN_MSGS = 4               # receive my own msgs (default:off)
    CAN_RAW_FD_FRAMES = 5                   # allow CAN FD frames (default:off)

    # noinspection PyPep8Naming
    class sockaddr_can(ctypes.Structure):
        """
        typedef __u32 canid_t;
        struct sockaddr_can {
            sa_family_t can_family;
            int         can_ifindex;
            union {
                struct { canid_t rx_id, tx_id; } tp;
            } can_addr;
        };
        """
        _fields_ = [
            ("can_family", ctypes.c_uint16),
            ("can_ifindex", ctypes.c_int),
            ("can_addr_tp_rx_id", ctypes.c_uint32),
            ("can_addr_tp_tx_id", ctypes.c_uint32)
        ]

    # noinspection PyPep8Naming
    class can_frame(ctypes.Structure):
        """
        typedef __u32 canid_t;
        struct can_frame {
            canid_t can_id;
            __u8    can_dlc;
            __u8    data[8] __attribute__((aligned(8)));
        };
        """
        _fields_ = [
            ("can_id", ctypes.c_uint32),
            ("can_dlc", ctypes.c_uint8),
            ("_pad", ctypes.c_ubyte * 3),
            ("data", ctypes.c_uint8 * 8)
        ]

    class CANSocket(object):
        def __init__(self, fd):
            if fd < 0:
                raise DriverError('Invalid socket fd')
            self.fd = fd

        def recv(self, bufsize, flags=None):
            frame = can_frame()
            nbytes = libc.read(self.fd, ctypes.byref(frame),
                               sys.getsizeof(frame))
            return ctypes.string_at(ctypes.byref(frame),
                                    ctypes.sizeof(frame))[0:nbytes]

        def send(self, data, flags=None):
            frame = can_frame()
            ctypes.memmove(ctypes.byref(frame), data,
                           ctypes.sizeof(frame))
            return libc.write(self.fd, ctypes.byref(frame),
                              ctypes.sizeof(frame))

        def fileno(self):
            return self.fd

        def close(self):
            libc.close(self.fd)

    def get_socket(ifname):
        socket_fd = libc.socket(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        if socket_fd < 0:
            raise DriverError('Could not open socket')

        libc.fcntl(socket_fd, fcntl.F_SETFL, os.O_NONBLOCK)

        ifidx = libc.if_nametoindex(ifname)
        if ctypes.get_errno() != 0:
            raise DriverError('Could not determine iface index [errno %s]' % ctypes.get_errno())

        addr = sockaddr_can(AF_CAN, ifidx)
        error = libc.bind(socket_fd, ctypes.byref(addr), ctypes.sizeof(addr))
        if error != 0:
            raise DriverError('Could not bind socket [errno %s]' % ctypes.get_errno())

        return CANSocket(socket_fd)


# from linux/can.h
CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF


class SocketCAN(object):
    FRAME_FORMAT = '=IB3x8s'
    FRAME_SIZE = 16
    TIMEVAL_FORMAT = '@LL'

    def __init__(self, interface, **_extras):
        self.socket = get_socket(interface)
        self.poll = select.poll()
        self.poll.register(self.socket.fileno())

    def close(self, callback=None):
        self.socket.close()

    def receive(self, timeout=None):
        timeout = -1 if timeout is None else (timeout * 1000)

        self.poll.modify(self.socket.fileno(), select.POLLIN | select.POLLPRI)
        if self.poll.poll(timeout):
            # Reading the packet itself
            packet_raw = self.socket.recv(self.FRAME_SIZE)
            can_id, can_dlc, can_data = struct.unpack(self.FRAME_FORMAT, packet_raw)

            # Reading the timestamp
            ts_raw = fcntl.ioctl(self.socket, SIOCGSTAMP, struct.pack(self.TIMEVAL_FORMAT, 0, 0))
            sec, usec = struct.unpack(self.TIMEVAL_FORMAT, ts_raw)
            timestamp = sec + usec * 1e-6

            # Converting the timestamp into the local time base
            # TODO: implement the timestamp conversion

            return RxFrame(can_id & CAN_EFF_MASK, can_data[0:can_dlc], bool(can_id & CAN_EFF_FLAG))

    def send(self, message_id, message, extended=False):
        if extended:
            message_id |= CAN_EFF_FLAG

        message_pad = bytes(message) + b'\x00' * (8 - len(message))
        self.socket.send(struct.pack(self.FRAME_FORMAT, message_id, len(message), message_pad))

